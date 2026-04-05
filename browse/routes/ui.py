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


@blueprint.route("abs/<px_id>")
def abstract(px_id: str) -> Response:
    """Abstract page for a paper."""
    from browse.services.papers import get_paper_by_id
    paper = get_paper_by_id(px_id)
    if paper is None:
        return render_template("abs/not_found.html", px_id=px_id), status.NOT_FOUND, {}
    # Format date in AOE (UTC-12): "Tue, 10 Feb 2026 19:00:00 AOE"
    import re as _re
    from datetime import timedelta, timezone
    try:
        dt = datetime.strptime(paper["date"], "%Y-%m-%d %H:%M:%S")
        aoe = timezone(timedelta(hours=-12))
        dt_aoe = dt.replace(tzinfo=timezone.utc).astimezone(aoe)
        date_formatted = dt_aoe.strftime("%a, %d %b %Y %H:%M:%S") + " AOE"
        year = str(dt.year)
    except (ValueError, KeyError):
        date_formatted = paper.get("date", "")
        year = ""
    # Generate BibTeX key: author2026firstword
    author_key = _re.sub(r'[^a-z]', '', paper.get("author", "").split()[0].lower()) if paper.get("author") else "unknown"
    title_word = _re.sub(r'[^a-z]', '', paper.get("title", "").split()[0].lower()) if paper.get("title") else "paper"
    bibtex_key = f"{author_key}{year}{title_word}"
    paper = {**paper, "date_formatted": date_formatted, "year": year, "bibtex_key": bibtex_key}
    return render_template("abs/abs.html", paper=paper), status.OK, {}


@blueprint.route("bibtex/<px_id>")
def bibtex(px_id: str) -> Response:
    """Return BibTeX citation for a paper."""
    from browse.services.papers import get_paper_by_id, paper_to_bibtex
    paper = get_paper_by_id(px_id)
    if paper is None:
        return "Paper not found", status.NOT_FOUND, {}
    return Response(paper_to_bibtex(paper), mimetype="text/plain")


@blueprint.route("pdf/<px_id>")
def pdf(px_id: str) -> Response:
    """Serve PDF from GCS bucket inline."""
    import urllib.request
    from browse.services.papers import get_paper_by_id
    paper = get_paper_by_id(px_id)
    if paper is None:
        return "Paper not found", status.NOT_FOUND, {}
    try:
        with urllib.request.urlopen(paper["pdf_url"], timeout=15) as resp:
            pdf_data = resp.read()
    except Exception:
        return "PDF not available", status.NOT_FOUND, {}
    return Response(pdf_data, mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={px_id}.pdf"})


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
