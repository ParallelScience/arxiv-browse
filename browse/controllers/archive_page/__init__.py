"""Archive landing page."""

import json
import os
from typing import Any, Dict, List, Optional
from http import HTTPStatus as status

from browse.services.papers import get_all_current_papers

Response = tuple[Dict[str, Any], int, Dict[str, Any]]

TAXONOMY_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "arxiv_taxonomy.json")

# Archive display names
ARCHIVE_NAMES = {
    "astro-ph": "Astrophysics",
    "cond-mat": "Condensed Matter",
    "gr-qc": "General Relativity and Quantum Cosmology",
    "hep-ex": "High Energy Physics - Experiment",
    "hep-lat": "High Energy Physics - Lattice",
    "hep-ph": "High Energy Physics - Phenomenology",
    "hep-th": "High Energy Physics - Theory",
    "math-ph": "Mathematical Physics",
    "nlin": "Nonlinear Sciences",
    "nucl-ex": "Nuclear Experiment",
    "nucl-th": "Nuclear Theory",
    "physics": "Physics",
    "quant-ph": "Quantum Physics",
    "math": "Mathematics",
    "cs": "Computer Science",
    "q-bio": "Quantitative Biology",
    "q-fin": "Quantitative Finance",
    "stat": "Statistics",
    "eess": "Electrical Engineering and Systems Science",
    "econ": "Economics",
}


def _load_taxonomy() -> dict:
    if os.path.exists(TAXONOMY_PATH):
        with open(TAXONOMY_PATH) as f:
            return json.load(f)
    return {}


def get_archive(archive_id: Optional[str]) -> Response:
    """Gets archive page."""
    if not archive_id or archive_id == "list":
        return {"template": "archive/archive_list_all.html", "bad_archive": "list", "archives": []}, status.NOT_FOUND, {}

    archive_name = ARCHIVE_NAMES.get(archive_id)
    if not archive_name:
        return {"template": "archive/archive_list_all.html", "bad_archive": archive_id, "archives": []}, status.NOT_FOUND, {}

    taxonomy = _load_taxonomy()
    papers = get_all_current_papers()

    # Find active categories for this archive
    active_categories = set()
    for paper in papers:
        primary = paper.get("primary_category", "")
        if primary:
            active_categories.add(primary)
        for sec in paper.get("secondary_categories", []):
            if sec:
                active_categories.add(sec)

    # Build category list for this archive
    categories = []
    for cat_code in sorted(taxonomy.keys()):
        is_match = (cat_code == archive_id or
                   cat_code.startswith(archive_id + ".") or
                   cat_code.startswith(archive_id + "-"))
        if is_match and cat_code in active_categories:
            categories.append({
                "code": cat_code,
                "name": taxonomy[cat_code]["name"],
                "description": taxonomy[cat_code].get("description", ""),
            })

    # Find earliest paper date in this archive
    import re
    earliest = None
    for paper in papers:
        all_cats = [paper.get("primary_category", "")] + paper.get("secondary_categories", [])
        for cat in all_cats:
            if cat == archive_id or cat.startswith(archive_id + ".") or cat.startswith(archive_id + "-"):
                date_str = paper.get("date", "")
                match = re.match(r"(\d{4})-(\d{2})", date_str)
                if match:
                    year, month = int(match.group(1)), int(match.group(2))
                    if earliest is None or (year, month) < earliest:
                        earliest = (year, month)
                break

    if earliest:
        from datetime import date
        since = date(earliest[0], earliest[1], 1).strftime("%B %Y")
    else:
        since = "2026"

    data = {
        "archive_id": archive_id,
        "archive_name": archive_name,
        "categories": categories,
        "since": since,
        "template": "archive/single_archive.html",
    }
    return data, status.OK, {}
