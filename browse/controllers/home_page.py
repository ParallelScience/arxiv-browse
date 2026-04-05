"""Handle requests for the home page."""

import json
import os
from typing import Any, Dict, Tuple
from http import HTTPStatus as status

from browse.services.papers import get_all_current_papers, _load_taxonomy

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


def get_home_page() -> Response:
    """Build the home page data with dynamic category listing."""
    taxonomy = _load_taxonomy()
    papers = get_all_current_papers()

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
