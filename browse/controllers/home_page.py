"""Handle requests for the home page."""

import json
import os
from collections import defaultdict
from typing import Any, Dict, Tuple
from http import HTTPStatus as status

Response = Tuple[Dict[str, Any], int, Dict[str, Any]]

# arXiv top-level groups in display order
GROUPS_ORDER = [
    ("Physics", [
        "astro-ph", "cond-mat", "gr-qc", "hep-ex", "hep-lat", "hep-ph", "hep-th",
        "math-ph", "nlin", "nucl-ex", "nucl-th", "physics", "quant-ph",
    ]),
    ("Mathematics", ["math"]),
    ("Computer Science", ["cs"]),
    ("Quantitative Biology", ["q-bio"]),
    ("Quantitative Finance", ["q-fin"]),
    ("Statistics", ["stat"]),
    ("Electrical Engineering and Systems Science", ["eess"]),
    ("Economics", ["econ"]),
]

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

TAXONOMY_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "denario", "data", "arxiv_taxonomy.json")
# Fallback: try a local copy
TAXONOMY_PATH_LOCAL = os.path.join(os.path.dirname(__file__), "..", "data", "arxiv_taxonomy.json")
PAPERS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "papers.json")


def _load_taxonomy() -> dict:
    for path in [TAXONOMY_PATH_LOCAL, TAXONOMY_PATH]:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    return {}


def _load_papers() -> list[dict]:
    if os.path.exists(PAPERS_PATH):
        with open(PAPERS_PATH) as f:
            return json.load(f)
    return []


def get_home_page() -> Response:
    """Build the home page data with dynamic category listing."""
    taxonomy = _load_taxonomy()
    papers = _load_papers()

    # Collect all categories that have at least one paper
    active_categories = set()
    for paper in papers:
        primary = paper.get("primary_category", "")
        if primary:
            active_categories.add(primary)
        for sec in paper.get("secondary_categories", []):
            if sec:
                active_categories.add(sec)

    # Derive active archives from active categories
    active_archives = set()
    for cat in active_categories:
        # "physics.class-ph" -> "physics", "astro-ph.CO" -> "astro-ph", "gr-qc" -> "gr-qc"
        if "." in cat:
            archive = cat.rsplit(".", 1)[0]
            # Handle cases like "astro-ph.CO" where archive is "astro-ph"
            active_archives.add(archive)
        else:
            active_archives.add(cat)

    # Build the group structure for the template
    groups = []
    for group_name, archive_ids in GROUPS_ORDER:
        archives = []
        for archive_id in archive_ids:
            if archive_id not in active_archives:
                continue

            archive_name = ARCHIVE_NAMES.get(archive_id, archive_id)

            # Find subcategories for this archive that have papers
            subcats = []
            for cat_code in sorted(taxonomy.keys()):
                is_match = (cat_code == archive_id or
                           cat_code.startswith(archive_id + ".") or
                           cat_code.startswith(archive_id + "-"))
                if is_match and cat_code in active_categories:
                    subcats.append({
                        "code": cat_code,
                        "name": taxonomy[cat_code]["name"],
                    })

            archives.append({
                "id": archive_id,
                "name": archive_name,
                "subcategories": subcats,
            })

        if archives:
            groups.append({
                "name": group_name,
                "archives": archives,
            })

    response_data: Dict[str, Any] = {
        "groups": groups,
        "paper_count": len(papers),
    }
    return response_data, status.OK, {}
