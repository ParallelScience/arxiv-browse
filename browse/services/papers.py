"""Serve papers from the ParallelScience papers.json index."""

import json
import os
from typing import Optional

_papers_cache: list[dict] | None = None
_taxonomy_cache: dict | None = None
PAPERS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "papers.json")
TAXONOMY_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "arxiv_taxonomy.json")


def _load_papers() -> list[dict]:
    global _papers_cache
    if _papers_cache is None:
        if os.path.exists(PAPERS_PATH):
            with open(PAPERS_PATH) as f:
                _papers_cache = json.load(f)
        else:
            _papers_cache = []
    return _papers_cache


def _load_taxonomy() -> dict:
    global _taxonomy_cache
    if _taxonomy_cache is None:
        if os.path.exists(TAXONOMY_PATH):
            with open(TAXONOMY_PATH) as f:
                _taxonomy_cache = json.load(f)
        else:
            _taxonomy_cache = {}
    return _taxonomy_cache


def get_papers_for_category(context: str) -> Optional[list[dict]]:
    """Return papers matching a category (primary or secondary), or all papers for an archive.

    Returns None if context doesn't match any known category/archive with papers.
    """
    papers = _load_papers()

    # Filter papers that match this category (primary or secondary)
    matched = [p for p in papers if _paper_matches_category(p, context)]

    if matched:
        return matched

    # No matches — check if this is even a valid category in our data
    all_categories = set()
    for p in papers:
        primary = p.get("primary_category", "")
        if primary:
            all_categories.add(primary)
            # Add the archive too (e.g., "physics" from "physics.class-ph")
            if "." in primary:
                all_categories.add(primary.rsplit(".", 1)[0])
        for sec in p.get("secondary_categories", []):
            if sec:
                all_categories.add(sec)
                if "." in sec:
                    all_categories.add(sec.rsplit(".", 1)[0])

    if context in all_categories:
        return []  # Valid category but no papers right now

    return None  # Unknown category


def _paper_matches_category(paper: dict, context: str) -> bool:
    """Check if a paper belongs to a category or archive."""
    primary = paper.get("primary_category", "")
    secondary = paper.get("secondary_categories", [])
    all_cats = [primary] + secondary

    for cat in all_cats:
        if cat == context:
            return True
        # Archive match: "physics" matches "physics.class-ph"
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
    """Return all papers by a given author."""
    return [p for p in _load_papers() if p.get("author", "").lower() == author.lower()]


def get_paper_by_id(px_id: str) -> Optional[dict]:
    """Return a single paper by its PX ID (e.g., '2604.00001')."""
    for p in _load_papers():
        if p.get("px_id") == px_id:
            return p
    return None


def paper_to_bibtex(paper: dict) -> str:
    """Generate a BibTeX entry from a paper dict."""
    import re
    date = paper.get("date", "")
    match = re.match(r"(\d{4})-(\d{2})", date)
    year = match.group(1) if match else "2026"
    month_num = int(match.group(2)) if match else 1
    months = ["jan", "feb", "mar", "apr", "may", "jun",
              "jul", "aug", "sep", "oct", "nov", "dec"]
    month = months[month_num - 1] if 1 <= month_num <= 12 else "jan"
    px_id = paper.get("px_id", "unknown")
    return (
        f"@article{{PX:{px_id},\n"
        f"  title   = {{{paper.get('title', '')}}},\n"
        f"  author  = {{{paper.get('author', '')}}},\n"
        f"  year    = {{{year}}},\n"
        f"  month   = {{{month}}},\n"
        f"  url     = {{{paper.get('pages_url', '')}}},\n"
        f"  journal = {{Parallel ArXiv}}\n"
        f"}}\n"
    )
