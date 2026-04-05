"""GitHub webhook endpoint for real-time paper ingestion."""

import hashlib
import hmac
import logging
import os

from flask import Blueprint, Response, current_app, request

blueprint = Blueprint("webhook", __name__, url_prefix="/webhook")
log = logging.getLogger(__name__)


def _verify_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """Verify the X-Hub-Signature-256 HMAC."""
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


@blueprint.route("/github", methods=["POST"])
def github_webhook() -> Response:
    """Handle GitHub org-level webhook events.

    Listens for ``page_build`` events with status ``built`` and scrapes
    the repo to upsert the paper into the database.
    """
    secret = current_app.config.get("WEBHOOK_SECRET", "")

    # Verify HMAC signature if a secret is configured
    if secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_signature(request.data, signature, secret):
            log.warning("Invalid webhook signature")
            return Response("Forbidden", status=403)

    event = request.headers.get("X-GitHub-Event", "")

    # Acknowledge ping events (sent when webhook is first created)
    if event == "ping":
        return Response("pong", status=200)

    # Only process page_build events
    if event != "page_build":
        return Response("ignored", status=204)

    payload = request.get_json(silent=True)
    if not payload:
        return Response("bad payload", status=400)

    # Only act on successful builds
    build = payload.get("build", {})
    if build.get("status") != "built":
        return Response("not a successful build", status=204)

    repo_name = payload.get("repository", {}).get("name", "")
    org_name = payload.get("organization", {}).get("login", "")
    if not repo_name or not org_name:
        return Response("missing repo/org", status=400)

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

    # Persist DB to GCS so it survives container restarts
    if action != "unchanged":
        sync_to_gcs()

    return Response(
        f'{{"px_id":"{px_id}","version":{version},"action":"{action}"}}',
        status=200,
        mimetype="application/json",
    )
