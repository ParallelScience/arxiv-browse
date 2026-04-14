"""Single source of truth for turning parsed paper metadata + artifacts into
a durable DB row (and its citation graph).

Both the GitHub Pages webhook (via ``scraper.upsert_paper``) and the REST
API ``/api/v1/papers`` handler flow through ``ingest_one()``. Upstream
callers materialize bytes/text themselves — where those bytes came from
(a live HTTP fetch, an inline request body, or a GitHub raw URL) is not
this module's concern.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import urllib.request

from browse.services.id_registry import get_or_assign_id

log = logging.getLogger(__name__)


def fetch_bytes(url: str, timeout: int = 30) -> bytes | None:
    """Download *url* into memory. Returns ``None`` on any error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ParallelArxiv/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as exc:
        log.warning("fetch_bytes failed for %s: %s", url, exc)
        return None


def _compute_content_hash(
    *,
    title: str,
    authors: str,
    date: str,
    abstract: str,
    primary_category: str,
    secondary_categories: list[str],
) -> str:
    """SHA-256 of the fields that drive version bumps (same shape as the
    legacy ``scraper.compute_content_hash`` so existing rows stay stable)."""
    payload = json.dumps({
        "title": title,
        "author": authors,
        "date": date,
        "abstract": abstract,
        "primary_category": primary_category,
        "secondary_categories": sorted(secondary_categories),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _store_pdf(
    pdf_data: bytes,
    px_id: str,
    version: int,
    local_dir: str | None = None,
    gcs_bucket: str | None = None,
) -> str:
    """Persist PDF bytes to local disk and/or GCS. Returns a public URL, or
    an empty string if neither destination is configured."""
    if not pdf_data:
        return ""
    filename = f"{px_id}v{version}.pdf"

    if local_dir:
        os.makedirs(local_dir, exist_ok=True)
        with open(os.path.join(local_dir, filename), "wb") as f:
            f.write(pdf_data)

    if gcs_bucket:
        try:
            from google.cloud import storage
            client = storage.Client()
            bucket = client.bucket(gcs_bucket)
            blob = bucket.blob(filename)
            blob.upload_from_string(pdf_data, content_type="application/pdf")
            return f"https://storage.googleapis.com/{gcs_bucket}/{filename}"
        except Exception as exc:
            log.error("GCS PDF upload failed: %s", exc)
            return ""

    if local_dir:
        return os.path.join(local_dir, filename)
    return ""


def ingest_one(
    conn: sqlite3.Connection,
    *,
    org: str,
    slug: str,
    title: str,
    authors: str,
    date: str,
    primary_category: str,
    abstract: str,
    secondary_categories: list[str] | None = None,
    pdf_data: bytes | None = None,
    bib_text: str | None = None,
    pages_url: str = "",
    github_url: str = "",
    local_pdf_dir: str | None = None,
    gcs_bucket: str | None = None,
) -> dict:
    """Ingest one paper.

    Returns ``{"px_id", "version", "action", "citations_count"}``. ``action``
    is ``"new"``, ``"updated"``, or ``"unchanged"``. Citations are re-ingested
    on every call when ``bib_text`` is provided, even if the paper content
    itself is unchanged — so a bib-only push refreshes the citation graph.
    """
    secondary_categories = list(secondary_categories or [])

    content_hash = _compute_content_hash(
        title=title, authors=authors, date=date, abstract=abstract,
        primary_category=primary_category,
        secondary_categories=secondary_categories,
    )

    px_id = get_or_assign_id(conn, org, slug, date)

    existing = conn.execute(
        "SELECT version, content_hash FROM papers "
        "WHERE px_id = ? AND is_current = 1",
        (px_id,),
    ).fetchone()

    if existing and existing["content_hash"] == content_hash:
        citations_count = _upsert_citations_if_present(conn, px_id, bib_text)
        return {
            "px_id": px_id,
            "version": existing["version"],
            "action": "unchanged",
            "citations_count": citations_count,
        }

    if existing:
        new_version = existing["version"] + 1
        conn.execute(
            "UPDATE papers SET is_current = 0 WHERE px_id = ? AND version = ?",
            (px_id, existing["version"]),
        )
        action = "updated"
    else:
        new_version = 1
        action = "new"

    pdf_gcs_url = ""
    if pdf_data:
        pdf_gcs_url = _store_pdf(
            pdf_data, px_id, new_version,
            local_dir=local_pdf_dir, gcs_bucket=gcs_bucket,
        )

    conn.execute(
        "INSERT INTO papers "
        "(px_id, version, title, author, date, abstract, "
        " primary_category, secondary_categories, source_org, repo, "
        " pages_url, github_url, pdf_url, is_current, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            px_id, new_version, title, authors, date, abstract,
            primary_category, json.dumps(secondary_categories),
            org, slug, pages_url, github_url, pdf_gcs_url, content_hash,
        ),
    )
    conn.commit()
    log.info("%s %s v%d (%s)", action.upper(), px_id, new_version, title[:60])

    citations_count = _upsert_citations_if_present(conn, px_id, bib_text)
    return {
        "px_id": px_id,
        "version": new_version,
        "action": action,
        "citations_count": citations_count,
    }


def _upsert_citations_if_present(
    conn: sqlite3.Connection, px_id: str, bib_text: str | None
) -> int:
    if not bib_text:
        return 0
    from browse.services.citations import parse_bib_entries, upsert_citations
    entries = parse_bib_entries(bib_text)
    if not entries:
        return 0
    return upsert_citations(conn, px_id, entries)
