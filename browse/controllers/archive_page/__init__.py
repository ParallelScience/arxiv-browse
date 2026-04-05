"""Archive landing page."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from http import HTTPStatus as status

from arxiv.taxonomy.definitions import ARCHIVES, ARCHIVES_ACTIVE, ARCHIVES_SUBSUMED, CATEGORIES
from arxiv.taxonomy.category import Category, Archive

from browse.controllers import Response


def get_archive(archive_id: Optional[str]) -> Response:
    """Gets archive page."""
    data: Dict[str, Any] = {}
    response_headers: Dict[str, Any] = {}

    if not archive_id or archive_id == "list":
        return archive_index("list", status_in=status.OK)

    archive = ARCHIVES.get(archive_id, None)
    if not archive:
        category = CATEGORIES.get(archive_id, None)
        if category:
            archive = category.get_archive()
    if not archive:
        return archive_index(archive_id, status_in=status.NOT_FOUND)

    if not archive.is_active:
        subsuming_category = archive.get_canonical()
        if not isinstance(subsuming_category, Category):
            return archive_index(archive_id, status_in=status.NOT_FOUND)
        data["subsumed_id"] = archive.id
        data["subsuming_category"] = subsuming_category
        archive = subsuming_category.get_archive()

    data["archive"] = archive
    data["category_list"] = category_list(archive)
    data["template"] = "archive/single_archive.html"
    return data, status.OK, response_headers


def archive_index(bad_archive_id: str, status_in: int) -> Response:
    """Landing page for when there is no archive specified."""
    data: Dict[str, Any] = {}
    data["bad_archive"] = bad_archive_id
    archives = [v for k, v in ARCHIVES_ACTIVE.items() if not k.startswith("test")]
    archives.sort(key=lambda x: x.id)
    data["archives"] = archives
    data["template"] = "archive/archive_list_all.html"
    return data, status_in, {}


def category_list(archive: Archive) -> List[Category]:
    """Returns active categories for archive."""
    cats = [cat for cat in archive.get_categories()]
    cats.sort(key=lambda x: x.id)
    return cats
