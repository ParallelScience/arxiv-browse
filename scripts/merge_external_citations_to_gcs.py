#!/usr/bin/env python3
"""Merge fresh external-citation rows into the authoritative GCS DB.

The external-citation scraper runs on orion for ~25 min (arXiv rate limit
× ~500 papers/day). During that window webhooks on Cloud Run may write to
the authoritative ``gs://.../papers.db`` (new papers, new citations).
If we naively uploaded the scraper's local copy we'd stomp those writes.

Instead, this script:

1. Downloads the *current* GCS DB to a temp file (captures any webhook
   writes that landed during the scrape).
2. Copies only the scraper-owned rows — ``external_citations`` and
   ``external_scraper_state`` — from the local scraper DB into the fresh
   GCS snapshot.
3. Uploads the merged DB back to GCS.

The race is narrowed to the ~seconds between step 1 and step 3 rather
than the full scrape window.
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
import tempfile

log = logging.getLogger(__name__)


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    m = re.match(r"gs://([^/]+)/(.+)", uri)
    if not m:
        raise ValueError(f"Invalid GCS URI: {uri}")
    return m.group(1), m.group(2)


def _download_from_gcs(gcs_uri: str, dest: str) -> None:
    from google.cloud import storage
    bucket_name, blob_path = _parse_gcs_uri(gcs_uri)
    client = storage.Client()
    client.bucket(bucket_name).blob(blob_path).download_to_filename(dest)


def _upload_to_gcs(src: str, gcs_uri: str) -> None:
    from google.cloud import storage
    bucket_name, blob_path = _parse_gcs_uri(gcs_uri)
    client = storage.Client()
    client.bucket(bucket_name).blob(blob_path).upload_from_filename(src)


def _copy_external_rows(local_path: str, target_path: str) -> tuple[int, int]:
    """Copy external_citations + external_scraper_state from local to target.

    Uses ``INSERT OR REPLACE`` on both tables so re-running is idempotent
    and newer metadata (title/version) wins over stale rows. Returns
    ``(external_rows, state_rows)``.
    """
    # ATTACH the local DB read-only into the target connection so we can
    # do server-side copies without ferrying rows through Python.
    conn = sqlite3.connect(target_path)
    conn.execute("PRAGMA foreign_keys = OFF")  # we're not touching papers/id_registry
    try:
        # Make sure the target has the v2 schema (in case it's an older
        # snapshot that predates the migration).
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 2:
            # Run the same DDL as browse.services.database.SCHEMA_SQL for
            # the new tables only — avoids importing the package here.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS external_citations (
                    cited_px_id      TEXT NOT NULL,
                    source           TEXT NOT NULL,
                    external_id      TEXT NOT NULL,
                    external_version TEXT,
                    title            TEXT,
                    authors          TEXT,
                    year             TEXT,
                    match_method     TEXT NOT NULL,
                    posted_date      TEXT,
                    discovered_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    PRIMARY KEY (cited_px_id, source, external_id)
                );
                CREATE INDEX IF NOT EXISTS idx_ext_cite_cited ON external_citations(cited_px_id);
                CREATE INDEX IF NOT EXISTS idx_ext_cite_source ON external_citations(source, external_id);

                CREATE TABLE IF NOT EXISTS external_scraper_state (
                    source               TEXT PRIMARY KEY,
                    last_processed_date  TEXT NOT NULL,
                    last_run_at          TEXT NOT NULL
                );
                PRAGMA user_version = 2;
            """)

        conn.execute("ATTACH DATABASE ? AS src", (local_path,))
        cur = conn.execute("""
            INSERT OR REPLACE INTO external_citations
                (cited_px_id, source, external_id, external_version,
                 title, authors, year, match_method, posted_date, discovered_at)
            SELECT cited_px_id, source, external_id, external_version,
                   title, authors, year, match_method, posted_date, discovered_at
              FROM src.external_citations
        """)
        ext_rows = cur.rowcount

        cur = conn.execute("""
            INSERT OR REPLACE INTO external_scraper_state
                (source, last_processed_date, last_run_at)
            SELECT source, last_processed_date, last_run_at
              FROM src.external_scraper_state
        """)
        state_rows = cur.rowcount

        conn.commit()
        conn.execute("DETACH DATABASE src")
        return ext_rows, state_rows
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--local", required=True, help="Path to the scraper's local DB")
    ap.add_argument("--gcs-uri", required=True,
                    help="Authoritative GCS URI (e.g. gs://parallel-arxiv-pdfs/papers.db)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not os.path.exists(args.local):
        log.error("local DB not found: %s", args.local)
        return 2

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        target = tf.name
    try:
        log.info("downloading fresh %s -> %s", args.gcs_uri, target)
        _download_from_gcs(args.gcs_uri, target)

        ext_rows, state_rows = _copy_external_rows(args.local, target)
        log.info("merged %d external_citations rows + %d state rows", ext_rows, state_rows)

        log.info("uploading merged DB -> %s", args.gcs_uri)
        _upload_to_gcs(target, args.gcs_uri)
        log.info("done")
    finally:
        try:
            os.unlink(target)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
