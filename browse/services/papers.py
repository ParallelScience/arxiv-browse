"""Serve papers from the SQLite database."""

import json
import os
import re
import sqlite3
from typing import Optional

from browse.services.database import get_db

_taxonomy_cache: dict | None = None
TAXONOMY_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "arxiv_taxonomy.json")


def _load_taxonomy() -> dict:
    global _taxonomy_cache
    if _taxonomy_cache is None:
        if os.path.exists(TAXONOMY_PATH):
            with open(TAXONOMY_PATH) as f:
                _taxonomy_cache = json.load(f)
        else:
            _taxonomy_cache = {}
    return _taxonomy_cache


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a dict, parsing JSON fields."""
    d = dict(row)
    if isinstance(d.get("secondary_categories"), str):
        d["secondary_categories"] = json.loads(d["secondary_categories"])
    return d


def get_all_current_papers() -> list[dict]:
    """Return all current-version papers."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM papers WHERE is_current = 1 ORDER BY date DESC"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_papers_for_category(context: str) -> Optional[list[dict]]:
    """Return papers matching a category (primary or secondary), or all papers for an archive.

    Returns None if context doesn't match any known category/archive with papers.
    """
    papers = get_all_current_papers()

    matched = [p for p in papers if _paper_matches_category(p, context)]
    if matched:
        return matched

    # Check if this is a valid category in our data
    all_categories = set()
    for p in papers:
        primary = p.get("primary_category", "")
        if primary:
            all_categories.add(primary)
            if "." in primary:
                all_categories.add(primary.rsplit(".", 1)[0])
        for sec in p.get("secondary_categories", []):
            if sec:
                all_categories.add(sec)
                if "." in sec:
                    all_categories.add(sec.rsplit(".", 1)[0])

    if context in all_categories:
        return []

    return None


def _paper_matches_category(paper: dict, context: str) -> bool:
    """Check if a paper belongs to a category or archive."""
    primary = paper.get("primary_category", "")
    secondary = paper.get("secondary_categories", [])
    all_cats = [primary] + secondary

    for cat in all_cats:
        if cat == context:
            return True
        if "." in cat and cat.rsplit(".", 1)[0] == context:
            return True
    return False


def get_category_name(context: str) -> str:
    """Return the human-readable name for a category code."""
    taxonomy = _load_taxonomy()
    if context in taxonomy:
        return taxonomy[context]["name"]
    return context


def get_papers_by_author(author: str) -> list[dict]:
    """Return all current papers by a given author."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM papers WHERE is_current = 1 AND lower(author) = lower(?) "
        "ORDER BY date DESC",
        (author,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_paper_by_id(px_id: str, version: int | None = None) -> Optional[dict]:
    """Return a paper by its PX ID. If version is None, return the current version."""
    db = get_db()
    if version is not None:
        row = db.execute(
            "SELECT * FROM papers WHERE px_id = ? AND version = ?",
            (px_id, version),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM papers WHERE px_id = ? AND is_current = 1",
            (px_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_paper_versions(px_id: str) -> list[dict]:
    """Return all versions of a paper, ordered by version number."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM papers WHERE px_id = ? ORDER BY version",
        (px_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def paper_to_bibtex(paper: dict) -> str:
    """Generate a BibTeX entry from a paper dict."""
    date = paper.get("date", "")
    match = re.match(r"(\d{4})-(\d{2})", date)
    year = match.group(1) if match else "2026"
    month_num = int(match.group(2)) if match else 1
    months = ["jan", "feb", "mar", "apr", "may", "jun",
              "jul", "aug", "sep", "oct", "nov", "dec"]
    month = months[month_num - 1] if 1 <= month_num <= 12 else "jan"
    px_id = paper.get("px_id", "unknown")
    version = paper.get("version", 1)
    version_suffix = f"v{version}" if version > 1 else ""
    return (
        f"@article{{PX:{px_id},\n"
        f"  title   = {{{paper.get('title', '')}}},\n"
        f"  author  = {{{paper.get('author', '')}}},\n"
        f"  year    = {{{year}}},\n"
        f"  month   = {{{month}}},\n"
        f"  url     = {{https://papers.parallelscience.org/abs/{px_id}}},\n"
        f"  eprint  = {{{px_id}{version_suffix}}},\n"
        f"  archivePrefix = {{ParallelArXiv}},\n"
        f"  primaryClass  = {{{paper.get('primary_category', '')}}},\n"
        f"  journal = {{Parallel ArXiv}}\n"
        f"}}\n"
    )
