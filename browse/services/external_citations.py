"""External citation discovery: find papers on regular arXiv that cite PX papers.

The scraper fetches each day's new arXiv listings, downloads each paper's
source tarball, and scans its ``.bib`` + ``.tex`` content for references to
Parallel ArXiv papers. Matching reuses the same patterns as
:mod:`browse.services.citations` (``archivePrefix = ParallelArxiv``, citation
keys starting with ``PX:``, and ``papers.parallelscience.org/abs/...`` URLs).

Public API:
    query_arxiv_listings(date)            -> list[dict]
    fetch_arxiv_source(arxiv_id)          -> bytes | None
    extract_bib_and_tex(blob)             -> tuple[str, str]
    find_px_citations(bib, tex, conn)     -> list[dict]
    upsert_external_citations(...)        -> int
    get_external_state / set_external_state
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import logging
import re
import sqlite3
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from browse.services.citations import parse_bib_entries, resolve_cited_px_id

log = logging.getLogger(__name__)

ARXIV_API_URL = "http://export.arxiv.org/api/query"
ARXIV_SOURCE_URL = "http://export.arxiv.org/e-print/{arxiv_id}"
USER_AGENT = "ParallelArxiv-ExternalCitationScraper/1.0 (https://papers.parallelscience.org)"

MAX_SOURCE_BYTES = 50 * 1024 * 1024  # Skip anything >50 MB
ARXIV_PAGE_SIZE = 200
ARXIV_RATE_LIMIT_SECONDS = 3.0
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

PX_TEX_KEY_RE = re.compile(r"PX:(\d{4}\.\d{5})")
PX_TEX_URL_RE = re.compile(r"papers\.parallelscience\.org/abs/(\d{4}\.\d{5})")


# ---------------------------------------------------------------------------
# arXiv API listing
# ---------------------------------------------------------------------------

@dataclass
class ArxivPaper:
    arxiv_id: str      # e.g. '2604.12345' (no version)
    version: str       # e.g. 'v1'
    title: str
    authors: str       # comma-separated
    submitted: str     # ISO timestamp from atom feed

    def as_dict(self) -> dict:
        return {
            "arxiv_id": self.arxiv_id,
            "version": self.version,
            "title": self.title,
            "authors": self.authors,
            "submitted": self.submitted,
        }


def _parse_atom_feed(xml_text: str) -> list[ArxivPaper]:
    """Parse an arXiv Atom query response into ArxivPaper records."""
    papers: list[ArxivPaper] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("arXiv API returned unparseable feed: %s", exc)
        return papers

    for entry in root.findall("atom:entry", ATOM_NS):
        id_el = entry.find("atom:id", ATOM_NS)
        if id_el is None or not id_el.text:
            continue
        # id looks like http://arxiv.org/abs/2604.12345v1
        m = re.search(r"arxiv\.org/abs/([^v\s]+)(v\d+)?", id_el.text)
        if not m:
            continue
        arxiv_id = m.group(1)
        version = m.group(2) or "v1"

        title_el = entry.find("atom:title", ATOM_NS)
        title = (title_el.text or "").strip() if title_el is not None else ""
        # Collapse newlines/indent from the Atom feed
        title = re.sub(r"\s+", " ", title)

        authors = []
        for author in entry.findall("atom:author/atom:name", ATOM_NS):
            if author.text:
                authors.append(author.text.strip())
        authors_str = ", ".join(authors)

        published_el = entry.find("atom:published", ATOM_NS)
        submitted = (published_el.text or "").strip() if published_el is not None else ""

        papers.append(ArxivPaper(
            arxiv_id=arxiv_id,
            version=version,
            title=title,
            authors=authors_str,
            submitted=submitted,
        ))
    return papers


def query_arxiv_listings(
    date: dt.date,
    *,
    sleep: float = ARXIV_RATE_LIMIT_SECONDS,
    url_opener=urllib.request.urlopen,
) -> list[dict]:
    """Query arXiv for every paper submitted on ``date`` (UTC).

    The arXiv API caps results at ~2000/query; we paginate with ``start`` to
    be safe. ``url_opener`` is injectable for testing.
    """
    start = 0
    results: list[ArxivPaper] = []
    date_from = date.strftime("%Y%m%d") + "0000"
    date_to = date.strftime("%Y%m%d") + "2359"
    while True:
        params = {
            "search_query": f"submittedDate:[{date_from} TO {date_to}]",
            "start": str(start),
            "max_results": str(ARXIV_PAGE_SIZE),
            "sortBy": "submittedDate",
            "sortOrder": "ascending",
        }
        url = f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with url_opener(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            log.warning("arXiv API query failed (start=%d): %s", start, exc)
            break

        page = _parse_atom_feed(body)
        if not page:
            break
        results.extend(page)
        if len(page) < ARXIV_PAGE_SIZE:
            break
        start += ARXIV_PAGE_SIZE
        if sleep > 0:
            time.sleep(sleep)

    return [p.as_dict() for p in results]


# ---------------------------------------------------------------------------
# Source fetch + extraction
# ---------------------------------------------------------------------------

def fetch_arxiv_source(
    arxiv_id: str,
    *,
    max_bytes: int = MAX_SOURCE_BYTES,
    url_opener=urllib.request.urlopen,
) -> bytes | None:
    """Download the source tarball for an arXiv paper.

    Returns the raw bytes, or ``None`` if the paper is withdrawn (404),
    exceeds ``max_bytes``, or any other error occurs. arXiv redirects
    ``e-print`` URLs to a signed URL, so we follow redirects implicitly
    via urllib.
    """
    url = ARXIV_SOURCE_URL.format(arxiv_id=arxiv_id)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with url_opener(req, timeout=60) as resp:
            blob = resp.read(max_bytes + 1)
            if len(blob) > max_bytes:
                log.info("skip %s: source exceeds %d bytes", arxiv_id, max_bytes)
                return None
            return blob
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log.info("skip %s: 404 (withdrawn or unavailable)", arxiv_id)
        else:
            log.warning("failed to fetch %s source: HTTP %d", arxiv_id, exc.code)
        return None
    except Exception as exc:
        log.warning("failed to fetch %s source: %s", arxiv_id, exc)
        return None


def _safe_decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def extract_bib_and_tex(blob: bytes) -> tuple[str, str]:
    """Extract concatenated .bib and .tex text from an arXiv source blob.

    Supports three layouts arXiv uses:
    - tar.gz archive of multiple files (the common case)
    - gzipped single .tex file (submissions with one .tex, no tarball)
    - raw text (rare, e.g. already-decompressed or HTML withdrawal notice)

    Returns ``(bib_text, tex_text)``. Either may be empty.
    """
    if not blob:
        return "", ""

    bib_parts: list[str] = []
    tex_parts: list[str] = []

    # First, try tar. arXiv tarballs are almost always gzip'd, but some are
    # plain tar. tarfile.open auto-detects.
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                name = member.name.lower()
                if not (name.endswith(".tex") or name.endswith(".bib") or name.endswith(".bbl")):
                    continue
                try:
                    f = tf.extractfile(member)
                    if f is None:
                        continue
                    content = _safe_decode(f.read())
                except Exception:
                    continue
                if name.endswith(".bib"):
                    bib_parts.append(content)
                else:
                    tex_parts.append(content)
        return "\n".join(bib_parts), "\n".join(tex_parts)
    except tarfile.ReadError:
        pass

    # Try bare gzip (single-file .tex submission).
    try:
        decompressed = gzip.decompress(blob)
        return "", _safe_decode(decompressed)
    except (OSError, EOFError):
        pass

    # Last resort: treat as raw text.
    return "", _safe_decode(blob)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _bib_match_method(entry: dict) -> str:
    """Classify how a bib entry matched a PX paper (for the match_method col)."""
    if entry.get("archive_prefix", "").lower() == "parallelarxiv":
        return "bib_archive_prefix"
    if entry.get("citation_key", "").startswith("PX:"):
        return "bib_citation_key"
    return "bib_url"


def find_px_citations(
    bib_text: str,
    tex_text: str,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Return a list of ``{cited_px_id, match_method}`` dicts.

    Uses the existing bib parser to structure entries, then reuses the
    ``resolve_cited_px_id`` matcher so the three detection patterns stay in
    one place. Also regex-scans the ``.tex`` stream for ``PX:YYMM.NNNNN``
    and ``papers.parallelscience.org/abs/YYMM.NNNNN`` so inline ``\\href`` /
    ``\\url`` citations that never reached the .bib still count.

    Duplicates (same px_id matched twice) collapse to a single entry,
    preferring the first-seen match method.
    """
    seen: dict[str, str] = {}

    if bib_text:
        for entry in parse_bib_entries(bib_text):
            px_id = resolve_cited_px_id(entry, conn) if conn is not None else _resolve_px_no_db(entry)
            if px_id and px_id not in seen:
                seen[px_id] = _bib_match_method(entry)

    if tex_text:
        for pattern in (PX_TEX_KEY_RE, PX_TEX_URL_RE):
            for m in pattern.finditer(tex_text):
                px_id = m.group(1)
                if px_id not in seen:
                    seen[px_id] = "tex_regex"

    return [{"cited_px_id": px, "match_method": meth} for px, meth in seen.items()]


def _resolve_px_no_db(entry: dict) -> str | None:
    """DB-free version of resolve_cited_px_id (the DB arg is unused there anyway)."""
    # resolve_cited_px_id doesn't actually query the DB — it's just threaded
    # through for a future extension. Call it with None to avoid requiring a
    # live connection in pure-function contexts.
    return resolve_cited_px_id(entry, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def upsert_external_citations(
    conn: sqlite3.Connection,
    hits: list[dict],
    *,
    source: str,
    external_id: str,
    external_version: str | None,
    title: str | None,
    authors: str | None,
    year: str | None,
    posted_date: str | None,
) -> int:
    """Insert one row per (cited_px_id, source, external_id) hit.

    Idempotent: re-running for the same external_id replaces existing rows'
    mutable metadata (title/authors/version/posted_date) while preserving the
    original ``discovered_at``. Returns the number of rows affected.
    """
    if not hits:
        return 0

    count = 0
    for hit in hits:
        conn.execute(
            """
            INSERT INTO external_citations (
                cited_px_id, source, external_id, external_version,
                title, authors, year, match_method, posted_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cited_px_id, source, external_id) DO UPDATE SET
                external_version = excluded.external_version,
                title            = excluded.title,
                authors          = excluded.authors,
                year             = excluded.year,
                match_method     = excluded.match_method,
                posted_date      = excluded.posted_date
            """,
            (
                hit["cited_px_id"],
                source,
                external_id,
                external_version,
                title,
                authors,
                year,
                hit["match_method"],
                posted_date,
            ),
        )
        count += 1
    conn.commit()
    return count


def get_external_cited_by(conn: sqlite3.Connection, px_id: str) -> list[dict]:
    """Return external papers that cite ``px_id``, newest first.

    Ordered by ``posted_date`` desc (fallback to ``discovered_at`` when the
    listing feed didn't supply a posted date). Used by the abstract page's
    "Cited by external papers" section and the ParallelScienceAPI endpoint.
    """
    rows = conn.execute(
        """
        SELECT source, external_id, external_version, title, authors, year,
               match_method, posted_date, discovered_at
          FROM external_citations
         WHERE cited_px_id = ?
         ORDER BY COALESCE(posted_date, discovered_at) DESC, external_id DESC
        """,
        (px_id,),
    ).fetchall()
    return [dict(r) if hasattr(r, "keys") else dict(zip(
        ["source", "external_id", "external_version", "title", "authors",
         "year", "match_method", "posted_date", "discovered_at"], r
    )) for r in rows]


def get_external_cited_by_count(conn: sqlite3.Connection, px_id: str) -> int:
    """Return the number of distinct external papers citing ``px_id``."""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT source || ':' || external_id) AS n
          FROM external_citations
         WHERE cited_px_id = ?
        """,
        (px_id,),
    ).fetchone()
    if row is None:
        return 0
    return row["n"] if hasattr(row, "keys") else row[0]


def get_external_state(conn: sqlite3.Connection, source: str) -> str | None:
    """Return the last fully-processed date (ISO YYYY-MM-DD) for ``source``."""
    row = conn.execute(
        "SELECT last_processed_date FROM external_scraper_state WHERE source = ?",
        (source,),
    ).fetchone()
    if row is None:
        return None
    return row["last_processed_date"] if hasattr(row, "keys") else row[0]


def set_external_state(conn: sqlite3.Connection, source: str, date: dt.date) -> None:
    """Record that ``date`` has been fully processed for ``source``."""
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO external_scraper_state (source, last_processed_date, last_run_at)
        VALUES (?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            last_processed_date = excluded.last_processed_date,
            last_run_at         = excluded.last_run_at
        """,
        (source, date.isoformat(), now),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_day(
    conn: sqlite3.Connection,
    date: dt.date,
    *,
    source: str = "arxiv",
    sleep: float = ARXIV_RATE_LIMIT_SECONDS,
    dry_run: bool = False,
    url_opener=urllib.request.urlopen,
) -> dict:
    """Scrape one day of arXiv listings and upsert any PX citations found.

    Returns counters: ``{papers_scanned, sources_fetched, px_hits, citing_papers}``.
    Caller is responsible for calling ``set_external_state`` after.
    """
    stats = {
        "papers_scanned": 0,
        "sources_fetched": 0,
        "px_hits": 0,
        "citing_papers": 0,
    }
    papers = query_arxiv_listings(date, sleep=sleep, url_opener=url_opener)
    log.info("day %s: %d arXiv papers to scan", date.isoformat(), len(papers))

    for paper in papers:
        stats["papers_scanned"] += 1
        if sleep > 0:
            time.sleep(sleep)
        blob = fetch_arxiv_source(paper["arxiv_id"], url_opener=url_opener)
        if blob is None:
            continue
        stats["sources_fetched"] += 1

        bib_text, tex_text = extract_bib_and_tex(blob)
        hits = find_px_citations(bib_text, tex_text, conn)
        if not hits:
            continue

        stats["citing_papers"] += 1
        stats["px_hits"] += len(hits)
        log.info(
            "  %s %s -> %d PX match(es): %s",
            paper["arxiv_id"], paper["version"],
            len(hits), ", ".join(h["cited_px_id"] for h in hits),
        )

        if not dry_run:
            year = paper["submitted"][:4] if paper.get("submitted") else None
            upsert_external_citations(
                conn, hits,
                source=source,
                external_id=paper["arxiv_id"],
                external_version=paper["version"],
                title=paper["title"] or None,
                authors=paper["authors"] or None,
                year=year,
                posted_date=paper["submitted"] or None,
            )

    return stats
