"""GitHub webhook endpoint for real-time paper ingestion.

Approved orgs (``APPROVED_ORGS`` in config / env) may submit papers by
installing an org-level webhook on their GitHub org pointing at
``POST /webhook/github``. Each approved org uses its own HMAC secret,
configured via the ``WEBHOOK_SECRET_<ORG_UPPERCASE>`` env var (with
``WEBHOOK_SECRET`` as the fallback for the default ``ParallelScience`` org,
so the pre-multi-org deployment keeps working).
"""

import hashlib
import hmac
import logging
import os

from flask import Blueprint, Response, current_app, request

blueprint = Blueprint("webhook", __name__, url_prefix="/webhook")
log = logging.getLogger(__name__)


def _env_key_for_org(org: str) -> str:
    """Derive the per-org secret env var name. Hyphens in org names (e.g.
    ``AstroPilot-AI``) are mapped to underscores since POSIX env var names
    can't contain hyphens."""
    return f"WEBHOOK_SECRET_{org.upper().replace('-', '_')}"


def secret_for_org(org: str) -> str:
    """Return the webhook HMAC secret configured for *org*, or ``""`` if none.

    Resolution order:
        1. ``WEBHOOK_SECRET_<ORG_UPPERCASE>`` env var (hyphens → underscores,
           e.g. ``AstroPilot-AI`` → ``WEBHOOK_SECRET_ASTROPILOT_AI``)
        2. For ``ParallelScience``, fall back to the legacy ``WEBHOOK_SECRET``
           setting so the existing single-org deployment keeps working.
    """
    secret = os.environ.get(_env_key_for_org(org), "")
    if secret:
        return secret
    if org == "ParallelScience":
        return current_app.config.get("WEBHOOK_SECRET", "") or os.environ.get(
            "WEBHOOK_SECRET", ""
        )
    return ""


def _verify_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """Verify the X-Hub-Signature-256 HMAC."""
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


def _authorize(org: str, approved_orgs: list[str]) -> Response | None:
    """Reject if *org* isn't approved or its HMAC signature is invalid.

    Returns ``None`` when the request is authorized, or a 403 Response to
    short-circuit the handler. Keeping allowlist + signature validation
    behind the same check prevents timing-based org discovery and makes
    the caller flow linear.
    """
    if not org or org not in approved_orgs:
        log.warning("Rejecting webhook from unapproved org: %r", org)
        return Response("Forbidden", status=403)

    secret = secret_for_org(org)
    if not secret:
        log.warning("No webhook secret configured for approved org %r", org)
        return Response("Forbidden", status=403)

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(request.data, signature, secret):
        log.warning("Invalid webhook signature for org %r", org)
        return Response("Forbidden", status=403)

    return None


@blueprint.route("/github", methods=["POST"])
def github_webhook() -> Response:
    """Handle GitHub org-level webhook events.

    Listens for ``page_build`` events with status ``built`` and scrapes
    the repo to upsert the paper into the database. Only accepts events
    from orgs listed in ``APPROVED_ORGS``; each org's requests must be
    signed with that org's configured secret.
    """
    approved_orgs = current_app.config.get("APPROVED_ORGS", [])
    event = request.headers.get("X-GitHub-Event", "")
    payload = request.get_json(silent=True) or {}
    org_name = payload.get("organization", {}).get("login", "")

    # Ping events are sent once when the webhook is installed; validate the
    # signature against the claimed org and acknowledge.
    if event == "ping":
        if (err := _authorize(org_name, approved_orgs)) is not None:
            return err
        return Response("pong", status=200)

    # Only process page_build events — any other event is silently ignored
    # without auth so we don't leak which secrets are configured.
    if event != "page_build":
        return Response("ignored", status=204)

    if (err := _authorize(org_name, approved_orgs)) is not None:
        return err

    # Only act on successful builds
    build = payload.get("build", {})
    if build.get("status") != "built":
        return Response("not a successful build", status=204)

    repo_name = payload.get("repository", {}).get("name", "")
    if not repo_name:
        return Response("missing repo", status=400)

    log.info("page_build event for %s/%s", org_name, repo_name)

    from browse.services.database import get_db, sync_to_gcs
    from browse.services.scraper import scrape_single_repo, upsert_paper

    meta = scrape_single_repo(org_name, repo_name)
    if not meta:
        log.info("No paper metadata found for %s/%s", org_name, repo_name)
        return Response("no paper metadata", status=204)

    conn = get_db()
    gcs_bucket = current_app.config.get("GCS_BUCKET", "parallel-arxiv-pdfs")
    local_pdf_dir = current_app.config.get("PDF_LOCAL_DIR", "")
    # On Cloud Run /rds doesn't exist; skip local PDF storage
    if local_pdf_dir and not os.path.isdir(os.path.dirname(local_pdf_dir)):
        local_pdf_dir = None

    px_id, version, action = upsert_paper(
        conn, meta, org_name,
        local_pdf_dir=local_pdf_dir,
        gcs_bucket=gcs_bucket,
    )

    # Extract citations from bibliography
    cite_count = 0
    try:
        from browse.services.citations import scrape_citations
        cite_count = scrape_citations(conn, org_name, repo_name, px_id)
        log.info("Extracted %d citations for %s", cite_count, px_id)
    except Exception as exc:
        log.warning("Citation extraction failed for %s: %s", repo_name, exc)

    # Persist DB to GCS so it survives container restarts.
    # Sync when the paper changed OR when citations were (re)extracted,
    # since citation updates don't bump the paper version.
    if action != "unchanged" or cite_count > 0:
        sync_to_gcs()

    return Response(
        f'{{"px_id":"{px_id}","version":{version},"action":"{action}"}}',
        status=200,
        mimetype="application/json",
    )
