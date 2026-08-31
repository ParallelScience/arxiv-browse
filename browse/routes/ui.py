"""Routes for Parallel ArXiv."""
import re as _re
from datetime import datetime, timedelta, timezone
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


def _format_paper_for_template(paper: dict) -> dict:
    """Add display fields (date_formatted, year, version info) to a paper dict."""
    date_str = paper.get("date", "")
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            break
        except ValueError:
            continue

    if dt:
        date_formatted = dt.strftime("%a, %d %b %Y %H:%M:%S") + " AOE"
        date_submitted = dt.strftime("%d %b %Y")
        year = str(dt.year)
    else:
        date_formatted = date_str
        date_submitted = date_str
        year = ""

    from browse.services.papers import get_category_name, get_paper_versions
    primary_cat = paper.get("primary_category", "")
    primary_category_name = get_category_name(primary_cat) if primary_cat else ""

    # Get all versions for submission history
    versions = get_paper_versions(paper["px_id"])

    return {
        **paper,
        "date_formatted": date_formatted,
        "date_submitted": date_submitted,
        "year": year,
        "primary_category_name": primary_category_name,
        "versions": versions,
    }


def _external_cited_by(px_id: str) -> list[dict]:
    """Best-effort external-citations lookup for the abs page.

    Older DB snapshots synced before the v1→v2 migration won't have the
    ``external_citations`` table yet, so we swallow that error and hide
    the UI section rather than 500 the whole abs page.
    """
    from browse.services.database import get_db
    from browse.services.external_citations import get_external_cited_by
    try:
        return get_external_cited_by(get_db(), px_id)
    except Exception:
        return []


@blueprint.route("abs/<px_id>")
def abstract(px_id: str) -> Response:
    """Abstract page for a paper (current version)."""
    from browse.services.papers import get_paper_by_id
    paper = get_paper_by_id(px_id)
    if paper is None:
        return render_template("abs/not_found.html", px_id=px_id), status.NOT_FOUND, {}
    paper = _format_paper_for_template(paper)
    external_cited_by = _external_cited_by(px_id)
    return render_template(
        "abs/abs.html", paper=paper, external_cited_by=external_cited_by,
    ), status.OK, {}


@blueprint.route("abs/<px_id>v<int:version>")
def abstract_version(px_id: str, version: int) -> Response:
    """Abstract page for a specific version of a paper."""
    from browse.services.papers import get_paper_by_id
    paper = get_paper_by_id(px_id, version=version)
    if paper is None:
        return render_template("abs/not_found.html", px_id=px_id), status.NOT_FOUND, {}
    paper = _format_paper_for_template(paper)
    external_cited_by = _external_cited_by(px_id)
    return render_template(
        "abs/abs.html", paper=paper, external_cited_by=external_cited_by,
    ), status.OK, {}


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
    """Serve PDF from GCS bucket inline (current version)."""
    return _serve_pdf(px_id, version=None)


@blueprint.route("pdf/<px_id>v<int:version>")
def pdf_version(px_id: str, version: int) -> Response:
    """Serve a specific version's PDF."""
    return _serve_pdf(px_id, version=version)


_GCS_HTTPS_PREFIXES = (
    "https://storage.googleapis.com/",
    "http://storage.googleapis.com/",
    "https://storage.cloud.google.com/",
)


def _fetch_pdf_bytes(pdf_url: str) -> bytes:
    """Fetch the bytes behind a stored ``pdf_url``.

    PDFs are read through the authenticated ``google-cloud-storage`` client
    rather than the public ``storage.googleapis.com`` URL that ``_store_pdf``
    records. Anonymous reads of these buckets stopped working on 2026-07-11
    when org-level Public Access Prevention was enforced: it overrides the
    bucket's surviving ``allUsers``/``objectViewer`` grant, so every
    unauthenticated GET became a 403 and this route answered "PDF not
    available" for every paper. Same trap ``database._download_from_gcs``
    documents for the papers DB. Non-GCS URLs still go over plain HTTP.
    """
    for prefix in _GCS_HTTPS_PREFIXES:
        if pdf_url.startswith(prefix):
            rest = pdf_url[len(prefix):]
            break
    else:
        rest = pdf_url[len("gs://"):] if pdf_url.startswith("gs://") else ""
    if rest:
        bucket_name, _, blob_path = rest.partition("/")
        if bucket_name and blob_path:
            from google.cloud import storage
            client = storage.Client()
            return client.bucket(bucket_name).blob(blob_path).download_as_bytes()

    import urllib.request
    with urllib.request.urlopen(pdf_url, timeout=15) as resp:
        return resp.read()


def _serve_pdf(px_id: str, version: int | None) -> Response:
    from browse.services.papers import get_paper_by_id
    paper = get_paper_by_id(px_id, version=version)
    if paper is None:
        return "Paper not found", status.NOT_FOUND, {}
    if not paper.get("pdf_url"):
        return "PDF not available", status.NOT_FOUND, {}
    try:
        pdf_data = _fetch_pdf_bytes(paper["pdf_url"])
    except Exception as exc:
        print(f"[PX] PDF fetch failed for {px_id} ({paper['pdf_url']}): {exc}",
              flush=True)
        return "PDF not available", status.NOT_FOUND, {}
    v = paper.get("version", 1)
    return Response(pdf_data, mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={px_id}v{v}.pdf"})


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
