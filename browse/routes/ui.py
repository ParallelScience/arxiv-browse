"""Routes for Parallel ArXiv."""
from datetime import datetime
from typing import Optional
from http import HTTPStatus as status

from flask import (
    Blueprint,
    Response,
    redirect,
    render_template,
    url_for,
)
from werkzeug.exceptions import InternalServerError

from browse.controllers import archive_page, home_page

blueprint = Blueprint("browse", __name__, url_prefix="/")


@blueprint.app_context_processor
def inject_now():
    """Inject current datetime into request context."""
    return dict(request_datetime=datetime.now())


@blueprint.route("index", methods=["GET"])
@blueprint.route("/", methods=["GET"])
def home() -> Response:
    """Home page."""
    response, code, headers = home_page.get_home_page()
    if code == status.OK:
        return render_template("home/home.html", **response), code, headers
    raise InternalServerError("Unexpected error")


@blueprint.route(
    "list", defaults={"context": "", "subcontext": ""}, methods=["GET"],
    strict_slashes=False
)
@blueprint.route("list/<context>/<subcontext>", methods=["GET"])
def list_articles(context: str, subcontext: str) -> Response:
    """List articles by category."""
    from browse.services.papers import get_papers_for_category, get_category_name
    papers = get_papers_for_category(context)
    if papers is not None:
        return render_template("list/parallel_list.html",
                               context=get_category_name(context),
                               subcontext=subcontext,
                               papers=papers,
                               now=datetime.now().strftime("%a, %d %b %Y")), status.OK, {}
    return "", status.NOT_FOUND, {}


@blueprint.route("author/<author>")
def author_papers(author: str) -> Response:
    """List papers by a specific author."""
    from browse.services.papers import get_papers_by_author
    papers = get_papers_by_author(author)
    return render_template("list/parallel_list.html",
                           context=f"Author: {author}",
                           subcontext="author",
                           papers=papers,
                           now=datetime.now().strftime("%a, %d %b %Y")), status.OK, {}


@blueprint.route("archive")
@blueprint.route("archive/")
@blueprint.route("archive/<archive>", strict_slashes=False)
def archive(archive: Optional[str] = None):
    """Landing page for an archive."""
    response, code, headers = archive_page.get_archive(archive)
    if code == status.OK or code == status.NOT_FOUND:
        return render_template(response["template"], **response), code, headers
    elif code == status.MOVED_PERMANENTLY:
        return redirect(headers["Location"], code=code)
    return response, code, headers
