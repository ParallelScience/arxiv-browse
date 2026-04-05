"""Serve papers from the ParallelScience papers.json index."""

import json
import os
from typing import Optional

_papers_cache: list[dict] | None = None
PAPERS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "papers.json")

# Categories that map to our papers
PARALLEL_CATEGORIES = {
    "physics": "Physics",
    "physics.class-ph": "Classical Physics",
}


def _load_papers() -> list[dict]:
    global _papers_cache
    if _papers_cache is None:
        if os.path.exists(PAPERS_PATH):
            with open(PAPERS_PATH) as f:
                _papers_cache = json.load(f)
        else:
            _papers_cache = []
    return _papers_cache


def get_papers_for_category(context: str) -> Optional[list[dict]]:
    """Return papers if context matches our categories, else None."""
    if context not in PARALLEL_CATEGORIES:
        return None
    return _load_papers()


def get_category_name(context: str) -> str:
    """Return the display name for a category, or the raw context."""
    return PARALLEL_CATEGORIES.get(context, context)


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
