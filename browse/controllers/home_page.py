"""Handle requests for the home page."""

from typing import Any, Dict, Tuple
from http import HTTPStatus as status
from arxiv.taxonomy.definitions import GROUPS, CATEGORIES

Response = Tuple[Dict[str, Any], int, Dict[str, Any]]


def get_home_page() -> Response:
    """Get the data needed to generate the home page."""
    response_data: Dict[str, Any] = {}
    response_data['groups'] = GROUPS
    response_data['categories'] = CATEGORIES
    return response_data, status.OK, {}
