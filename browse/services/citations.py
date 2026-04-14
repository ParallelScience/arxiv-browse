"""Citation extraction from paper bibliography files.

Fetches .bib files from GitHub repos, parses BibTeX entries,
resolves cross-references to PX papers, and stores in the DB.
"""

import logging
import re
import sqlite3
import urllib.request

log = logging.getLogger(__name__)


def fetch_bib_file(org: str, repo: str) -> str | None:
    """Try to fetch the .bib file from a paper's GitHub Pages or raw repo."""
    candidates = [
        f"https://{org}.github.io/{repo}/bibliography.bib",
        f"https://raw.githubusercontent.com/{org}/{repo}/main/bibliography.bib",
        f"https://raw.githubusercontent.com/{org}/{repo}/master/bibliography.bib",
    ]
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ParallelArxiv/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    text = resp.read().decode("utf-8", errors="replace")
                    if "@" in text:  # basic sanity check
                        return text
        except Exception:
            continue
    return None


def parse_bib_entries(bib_text: str) -> list[dict]:
    """Parse BibTeX text into a list of entry dicts.

    Uses regex — no external dependency. Handles the regular BibTeX
    produced by Denario's paper module.
    """
    entries = []
    # Match @type{key, ... } — the closing } must be at the start of a line
    entry_pattern = re.compile(
        r"@(\w+)\{([^,]+),\s*(.*?)\n\}", re.DOTALL
    )
    field_pattern = re.compile(
        r"(\w+)\s*=\s*[\{\"](.+?)[\}\"](?:\s*,)?", re.DOTALL
    )

    for match in entry_pattern.finditer(bib_text):
        entry_type = match.group(1).lower()
        if entry_type in ("string", "comment", "preamble"):
            continue
        citation_key = match.group(2).strip()
        body = match.group(3)

        fields = {}
        for field_match in field_pattern.finditer(body):
            key = field_match.group(1).lower().strip()
            value = field_match.group(2).strip()
            value = re.sub(r"[\{\}]", "", value)
            fields[key] = value

        entries.append({
            "citation_key": citation_key,
            "entry_type": entry_type,
            "title": fields.get("title", ""),
            "author": fields.get("author", ""),
            "year": fields.get("year", ""),
            "doi": fields.get("doi", ""),
            "eprint": fields.get("eprint", ""),
            "archive_prefix": fields.get("archiveprefix", ""),
            "url": fields.get("url", ""),
        })
    return entries


def resolve_cited_px_id(entry: dict, conn: sqlite3.Connection) -> str | None:
    """Check if a BibTeX entry references a PX paper. Return px_id or None."""
    # Case 1: archivePrefix is ParallelArXiv
    if entry.get("archive_prefix", "").lower() == "parallelarxiv":
        eprint = entry.get("eprint", "")
        px_id = re.sub(r"v\d+$", "", eprint)
        if px_id:
            return px_id

    # Case 2: citation key starts with PX:
    if entry["citation_key"].startswith("PX:"):
        return entry["citation_key"][3:]

    # Case 3: URL contains parallelscience.org/abs/
    url = entry.get("url", "")
    m = re.search(r"parallelscience\.org/abs/(\d{4}\.\d{5})", url)
    if m:
        return m.group(1)

    return None


def extract_arxiv_id(entry: dict) -> str | None:
    """Extract arXiv ID from a BibTeX entry."""
    prefix = entry.get("archive_prefix", "").lower()
    eprint = entry.get("eprint", "")
    if prefix in ("arxiv", "") and eprint:
        return eprint

    url = entry.get("url", "")
    m = re.search(r"arxiv\.org/abs/(\S+)", url)
    if m:
        return m.group(1).rstrip("/")
    return None


def upsert_citations(conn: sqlite3.Connection, citing_px_id: str, entries: list[dict]) -> int:
    """Replace all citations for a paper with freshly parsed entries.

    Returns the number of distinct rows written. If the source .bib has
    duplicate citation keys (common in auto-generated bibliographies), the
    later occurrence wins — matching what ``INSERT OR REPLACE`` would do —
    and the count reflects distinct keys, not attempted inserts.
    """
    conn.execute("DELETE FROM citations WHERE citing_px_id = ?", (citing_px_id,))

    # Deduplicate on citation_key, preserving insertion order so the last
    # occurrence overwrites earlier ones (Python 3.7+ dicts retain order).
    by_key: dict[str, dict] = {}
    for entry in entries:
        by_key[entry["citation_key"]] = entry

    for entry in by_key.values():
        cited_px_id = resolve_cited_px_id(entry, conn)
        arxiv_id = extract_arxiv_id(entry) if not cited_px_id else None

        conn.execute(
            "INSERT INTO citations "
            "(citing_px_id, citation_key, cited_px_id, arxiv_id, doi, title, authors, year) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                citing_px_id,
                entry["citation_key"],
                cited_px_id,
                arxiv_id,
                entry.get("doi") or None,
                entry.get("title") or None,
                entry.get("author") or None,
                entry.get("year") or None,
            ),
        )

    conn.commit()
    return len(by_key)


def scrape_citations(conn: sqlite3.Connection, org: str, repo: str, px_id: str) -> int:
    """Fetch and parse the .bib file for a repo, storing citations in the DB.

    Returns the number of citations extracted, or 0 if no bib file found.
    """
    bib_text = fetch_bib_file(org, repo)
    if not bib_text:
        return 0

    entries = parse_bib_entries(bib_text)
    if not entries:
        return 0

    count = upsert_citations(conn, px_id, entries)
    log.info("Extracted %d citations for %s (%s/%s)", count, px_id, org, repo)
    return count
