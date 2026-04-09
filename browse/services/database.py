"""SQLite database for Parallel ArXiv papers.

On Cloud Run the app directory is read-only, so the DB is stored in GCS
and downloaded to /tmp on startup.  After any write (webhook upsert) the
DB is uploaded back to GCS so it survives container restarts.
"""

import logging
import os
import re
import shutil
import sqlite3

from flask import Flask, g

log = logging.getLogger(__name__)

_DB_PATH: str = ""
_GCS_DB_URI: str = ""  # e.g. gs://parallel-arxiv-pdfs/papers.db

SCHEMA_SQL = """
-- Persistent ID registry: append-only, one row per repo, never deleted
CREATE TABLE IF NOT EXISTS id_registry (
    repo        TEXT PRIMARY KEY,
    px_id       TEXT NOT NULL UNIQUE,
    yymm        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Next sequence number per month (avoids scanning id_registry for max)
CREATE TABLE IF NOT EXISTS id_sequence (
    yymm    TEXT PRIMARY KEY,
    next_n  INTEGER NOT NULL DEFAULT 1
);

-- Papers table with version tracking
CREATE TABLE IF NOT EXISTS papers (
    px_id               TEXT NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1,
    title               TEXT NOT NULL,
    author              TEXT NOT NULL,
    date                TEXT NOT NULL,
    abstract            TEXT NOT NULL,
    primary_category    TEXT NOT NULL,
    secondary_categories TEXT NOT NULL DEFAULT '[]',
    repo                TEXT NOT NULL,
    pages_url           TEXT NOT NULL,
    github_url          TEXT NOT NULL,
    pdf_url             TEXT NOT NULL,
    is_current          INTEGER NOT NULL DEFAULT 1,
    scraped_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    content_hash        TEXT,
    PRIMARY KEY (px_id, version),
    FOREIGN KEY (repo) REFERENCES id_registry(repo)
);

CREATE INDEX IF NOT EXISTS idx_papers_current ON papers(is_current) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS idx_papers_category ON papers(primary_category) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS idx_papers_author ON papers(author) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS idx_papers_repo ON papers(repo);

-- Citations extracted from paper bibliography files
CREATE TABLE IF NOT EXISTS citations (
    citing_px_id    TEXT NOT NULL,
    citation_key    TEXT NOT NULL,
    cited_px_id     TEXT,
    arxiv_id        TEXT,
    doi             TEXT,
    title           TEXT,
    authors         TEXT,
    year            TEXT,
    created_at      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (citing_px_id, citation_key)
);

CREATE INDEX IF NOT EXISTS idx_citations_cited_px ON citations(cited_px_id) WHERE cited_px_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_citations_citing ON citations(citing_px_id);
"""


def get_db_path() -> str:
    """Return the configured database path."""
    return _DB_PATH


def get_db() -> sqlite3.Connection:
    """Get a database connection for the current request (Flask context)."""
    if "px_db" not in g:
        g.px_db = _connect()
    return g.px_db


def get_db_standalone() -> sqlite3.Connection:
    """Get a database connection outside of Flask context (for CLI scripts)."""
    return _connect()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def close_db(e=None) -> None:
    """Close the per-request database connection."""
    db = g.pop("px_db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# GCS sync (uses google-cloud-storage, a transitive dep of arxiv-base)
# ---------------------------------------------------------------------------

def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    """Parse 'gs://bucket/path' into (bucket, blob_path)."""
    m = re.match(r"gs://([^/]+)/(.+)", uri)
    if not m:
        raise ValueError(f"Invalid GCS URI: {uri}")
    return m.group(1), m.group(2)


def _gcs_public_url() -> str:
    """Convert gs://bucket/path to https://storage.googleapis.com/bucket/path."""
    bucket_name, blob_path = _parse_gcs_uri(_GCS_DB_URI)
    return f"https://storage.googleapis.com/{bucket_name}/{blob_path}"


def _download_from_gcs() -> bool:
    """Download the DB from GCS to _DB_PATH via public URL. Returns True on success."""
    if not _GCS_DB_URI:
        print("[PX] GCS_DB_URI not set, skipping download", flush=True)
        return False
    try:
        import urllib.request
        url = _gcs_public_url()
        print(f"[PX] Downloading DB from {url}", flush=True)
        urllib.request.urlretrieve(url, _DB_PATH)
        size = os.path.getsize(_DB_PATH)
        import sqlite3 as _sql
        _c = _sql.connect(_DB_PATH)
        _count = _c.execute("SELECT count(*) FROM papers").fetchone()[0]
        _c.close()
        print(f"[PX] Downloaded DB: {size} bytes, {_count} papers", flush=True)
        return True
    except Exception as exc:
        print(f"[PX] FAILED to download DB from GCS: {exc}", flush=True)
        return False


def sync_to_gcs() -> bool:
    """Upload the current DB to GCS. Call after any write operation."""
    if not _GCS_DB_URI:
        return False
    # Checkpoint WAL into the main DB file before uploading
    conn = _connect()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    try:
        from google.cloud import storage
        bucket_name, blob_path = _parse_gcs_uri(_GCS_DB_URI)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.upload_from_filename(_DB_PATH)
        print(f"[PX] Uploaded DB to gs://{bucket_name}/{blob_path}", flush=True)
        return True
    except Exception as exc:
        print(f"[PX] FAILED to upload DB to GCS: {exc}", flush=True)
        return False


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def init_db(app: Flask) -> None:
    """Initialize the database for the Flask app.

    Resolution order for the DB file:
    1. If GCS_DB_URI is set, download from GCS to /tmp/papers.db
    2. Else if PX_DATABASE_PATH is writable, use it directly
    3. Else copy the baked-in DB from PX_DATABASE_PATH to /tmp
    """
    global _DB_PATH, _GCS_DB_URI

    _GCS_DB_URI = app.config.get("GCS_DB_URI", "") or os.environ.get("GCS_DB_URI", "")
    source_path = app.config.get(
        "PX_DATABASE_PATH",
        os.path.join(os.path.dirname(__file__), "..", "data", "papers.db"),
    )
    print(f"[PX] init_db: GCS_DB_URI={_GCS_DB_URI!r}, source={source_path}", flush=True)

    if _GCS_DB_URI:
        # Cloud Run path: work in /tmp, sync with GCS
        _DB_PATH = "/tmp/papers.db"
        if not os.path.exists(_DB_PATH):
            if not _download_from_gcs():
                # First deploy: seed from baked-in DB if it exists
                if os.path.exists(source_path):
                    shutil.copy2(source_path, _DB_PATH)
                    print(f"[PX] Seeded /tmp DB from baked-in {source_path}", flush=True)
                else:
                    print("[PX] No baked-in DB either, creating fresh", flush=True)
        else:
            print(f"[PX] /tmp/papers.db already exists ({os.path.getsize(_DB_PATH)} bytes), reusing", flush=True)
    else:
        # Local dev: use configured path, fall back to /tmp if read-only
        parent = os.path.dirname(source_path) or "."
        if os.access(parent, os.W_OK):
            _DB_PATH = source_path
        else:
            _DB_PATH = "/tmp/papers.db"
            if os.path.exists(source_path) and not os.path.exists(_DB_PATH):
                shutil.copy2(source_path, _DB_PATH)

    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)

    conn = _connect()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()

    app.teardown_appcontext(close_db)
    log.info("Initialized papers database at %s (GCS: %s)", _DB_PATH, _GCS_DB_URI or "none")


def init_standalone(db_path: str) -> None:
    """Initialize database for CLI use (no Flask app, no GCS)."""
    global _DB_PATH
    _DB_PATH = db_path
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = _connect()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
