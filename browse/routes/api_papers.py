"""REST API for bulk paper submission.

``POST /api/v1/papers`` — accepts a manifest of many papers in a single
request, authenticated with a per-org Bearer token.

The auth contract mirrors the GitHub webhook: the org isn't user-supplied,
it's derived by matching the presented token against each approved org's
``API_KEY_<ORG_UPPERCASE>`` env var (hyphens → underscores, same mapping
as :func:`browse.routes.webhook.org_env_key`). Matching the token to an
org proves both identity and authorization to submit on that org's behalf.

Two content types are supported, pick whichever fits the submitter:

``application/json``
    Body is a manifest object. Each paper's ``pdf`` and ``bib`` fields are
    objects with either ``{"url": "..."}`` (server fetches) or
    ``{"base64": "..."}`` (inline bytes, base64-encoded).

``multipart/form-data``
    A ``manifest`` form field holds the JSON manifest. Additional file
    parts named ``pdf_<slug>`` and ``bib_<slug>`` carry raw binary. The
    ``pdf``/``bib`` fields in the manifest can be omitted or set to
    ``{"multipart": true}`` as a hint.

Per-paper errors do not fail the whole request; the response lists each
entry's outcome independently and the summary counts ok vs failed.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os

from flask import Blueprint, current_app, jsonify, request

from browse.routes.webhook import org_env_key
from browse.services.ingest import fetch_bytes, ingest_one

blueprint = Blueprint("api_papers", __name__, url_prefix="/api/v1")
log = logging.getLogger(__name__)

REQUIRED_FIELDS = ("slug", "title", "authors", "date", "primary_category", "abstract")
TOKEN_PREFIX = "pxak_"


def api_key_for_org(org: str) -> str:
    """Return the API key configured for *org*, or empty string if none."""
    return os.environ.get(org_env_key("API_KEY", org), "")


def _match_token(token: str) -> str | None:
    """Walk APPROVED_ORGS and return the org whose API_KEY matches *token*.

    Constant-time comparison via ``hmac.compare_digest`` per org. Returns
    ``None`` if no org's key matches (or if the token format is wrong).
    """
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    for org in current_app.config.get("APPROVED_ORGS", []):
        expected = api_key_for_org(org)
        if expected and hmac.compare_digest(token, expected):
            return org
    return None


def _resolve_artifact(
    spec: dict | None,
    slug: str,
    kind: str,
    files_by_slug: dict[str, dict[str, bytes]],
) -> bytes | None:
    """Resolve one artifact (pdf or bib) to raw bytes.

    Lookup order: multipart upload first (explicit override via raw bytes),
    then ``spec["url"]``, then ``spec["base64"]``. Returns ``None`` if
    nothing resolved — the caller decides whether that's fatal.
    """
    uploaded = files_by_slug.get(kind, {}).get(slug)
    if uploaded is not None:
        return uploaded
    if not isinstance(spec, dict):
        return None
    if url := spec.get("url"):
        return fetch_bytes(url)
    if b64 := spec.get("base64"):
        try:
            return base64.b64decode(b64, validate=False)
        except Exception:
            return None
    return None


def _ingest_entry(
    conn,
    org: str,
    entry: dict,
    index: int,
    files_by_slug: dict[str, dict[str, bytes]],
) -> dict:
    """Validate and ingest one manifest entry. Returns a response row."""
    slug = entry.get("slug") if isinstance(entry, dict) else None
    if not isinstance(slug, str) or not slug:
        return {
            "index": index, "slug": None, "status": "error",
            "error": "missing or empty 'slug'",
        }

    missing = [f for f in REQUIRED_FIELDS if not entry.get(f)]
    if missing:
        return {
            "slug": slug, "status": "error",
            "error": f"missing required field(s): {', '.join(missing)}",
        }

    pdf_data = _resolve_artifact(entry.get("pdf"), slug, "pdf", files_by_slug)
    if not pdf_data:
        return {
            "slug": slug, "status": "error",
            "error": "pdf could not be resolved (required)",
        }

    bib_raw = _resolve_artifact(entry.get("bib"), slug, "bib", files_by_slug)
    bib_text = bib_raw.decode("utf-8", errors="replace") if bib_raw else None

    secondary = entry.get("secondary_categories") or []
    if not isinstance(secondary, list):
        secondary = []

    try:
        result = ingest_one(
            conn,
            org=org,
            slug=slug,
            title=entry["title"],
            authors=entry["authors"],
            date=entry["date"],
            primary_category=entry["primary_category"],
            secondary_categories=secondary,
            abstract=entry["abstract"],
            pdf_data=pdf_data,
            bib_text=bib_text,
            pages_url=entry.get("source_url") or "",
            github_url="",
            local_pdf_dir=current_app.config.get("PDF_LOCAL_DIR") or None,
            gcs_bucket=current_app.config.get("GCS_BUCKET", "parallel-arxiv-pdfs") or None,
        )
    except Exception as exc:
        log.exception("ingest_one failed for %s/%s", org, slug)
        return {"slug": slug, "status": "error", "error": f"ingest failed: {exc}"}

    return {
        "slug": slug,
        "status": "ok",
        "px_id": result["px_id"],
        "version": result["version"],
        "action": result["action"],
        "citations_count": result["citations_count"],
    }


@blueprint.route("/papers", methods=["POST"])
def submit_papers():
    """Accept a manifest of papers and ingest them into ParallelArxiv."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "missing Bearer token"}), 401

    token = auth[len("Bearer "):].strip()
    org = _match_token(token)
    if org is None:
        return jsonify({"error": "invalid API key"}), 401

    content_type = (request.content_type or "").split(";", 1)[0].strip().lower()
    files_by_slug: dict[str, dict[str, bytes]] = {"pdf": {}, "bib": {}}

    if content_type == "application/json":
        try:
            payload = request.get_json(force=True)
        except Exception as exc:
            return jsonify({"error": f"invalid JSON: {exc}"}), 400
    elif content_type == "multipart/form-data":
        try:
            payload = json.loads(request.form.get("manifest", "{}"))
        except Exception as exc:
            return jsonify({"error": f"invalid manifest JSON: {exc}"}), 400
        for field_name, file_storage in request.files.items():
            for kind in ("pdf", "bib"):
                prefix = f"{kind}_"
                if field_name.startswith(prefix):
                    slug = field_name[len(prefix):]
                    files_by_slug[kind][slug] = file_storage.read()
    else:
        return jsonify({"error": f"unsupported Content-Type: {content_type!r}"}), 415

    if not isinstance(payload, dict):
        return jsonify({"error": "manifest must be a JSON object"}), 400

    papers = payload.get("papers")
    if not isinstance(papers, list) or not papers:
        return jsonify({"error": "'papers' must be a non-empty list"}), 400

    from browse.services.database import get_db, sync_to_gcs

    conn = get_db()
    results = [
        _ingest_entry(conn, org, entry, idx, files_by_slug)
        for idx, entry in enumerate(papers)
    ]

    ok = sum(1 for r in results if r["status"] == "ok")
    changed = any(
        r["status"] == "ok" and r.get("action") != "unchanged"
        for r in results
    )
    any_citations = any(
        r["status"] == "ok" and r.get("citations_count", 0) > 0
        for r in results
    )
    if changed or any_citations:
        sync_to_gcs()

    return jsonify({
        "results": results,
        "summary": {"total": len(results), "ok": ok, "failed": len(results) - ok},
    }), 200
