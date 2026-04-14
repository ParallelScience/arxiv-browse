"""Tests for ``POST /api/v1/papers`` bulk submission endpoint.

Mirrors the hermetic style of ``test_px_multiorg.py``: bypasses the heavy
legacy-arxiv conftest fixtures by building a minimal Flask app wired only
to the ``api_papers`` and ``webhook`` blueprints plus the PX database.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
from flask import Flask

from browse.routes.api_papers import blueprint as api_bp
from browse.services import database as db_service


VALID_TOKEN = "pxak_" + "a" * 64
OTHER_TOKEN = "pxak_" + "b" * 64
ORG = "AstroPilot-AI"


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    """Minimal Flask app with a fresh PX database and the API blueprint."""
    db_path = tmp_path / "papers.db"
    monkeypatch.setattr(db_service, "_DB_PATH", "")
    db_service.init_standalone(str(db_path))

    app = Flask(__name__)
    app.config["APPROVED_ORGS"] = [ORG]
    app.config["PX_DATABASE_PATH"] = str(db_path)
    app.config["GCS_BUCKET"] = ""  # disable GCS upload in tests
    app.config["PDF_LOCAL_DIR"] = str(tmp_path / "pdfs")
    app.register_blueprint(api_bp)

    db_service._DB_PATH = str(db_path)
    app.teardown_appcontext(db_service.close_db)

    # Good key for ORG; no key for any other org.
    monkeypatch.setenv("API_KEY_ASTROPILOT_AI", VALID_TOKEN)

    return app


@pytest.fixture
def client(app):
    return app.test_client()


# Shared minimal payload ----------------------------------------------------

def _paper(slug="merger-trees-project4", **overrides) -> dict:
    p = {
        "slug": slug,
        "title": "A GNN Study of Dark Matter Halo Concentration",
        "authors": "Denario-0",
        "date": "2025-08-29",
        "primary_category": "astro-ph.CO",
        "secondary_categories": ["cs.LG"],
        "abstract": "We apply GNNs to predict the direction of dark matter halo concentration evolution.",
        "pdf": {"base64": base64.b64encode(b"%PDF-1.5\n%tiny test pdf\n").decode()},
    }
    p.update(overrides)
    return p


def _post_json(client, body: dict, token: str = VALID_TOKEN):
    return client.post(
        "/api/v1/papers",
        data=json.dumps(body),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_missing_bearer_returns_401(client):
    resp = client.post("/api/v1/papers", data=json.dumps({"papers": [_paper()]}),
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 401


def test_unknown_token_returns_401(client):
    resp = _post_json(client, {"papers": [_paper()]}, token=OTHER_TOKEN)
    assert resp.status_code == 401


def test_malformed_token_prefix_returns_401(client):
    resp = _post_json(client, {"papers": [_paper()]}, token="not-a-pxak-token")
    assert resp.status_code == 401


def test_valid_token_is_accepted(client):
    resp = _post_json(client, {"papers": [_paper()]})
    assert resp.status_code == 200


def test_hyphenated_org_uses_underscored_env_var(app, client, monkeypatch):
    """Regression: AstroPilot-AI must look up API_KEY_ASTROPILOT_AI (hyphen → underscore)."""
    # Clobber the hyphenated name (which would be unreachable anyway, but belt-and-braces):
    monkeypatch.delenv("API_KEY_ASTROPILOT-AI", raising=False)
    resp = _post_json(client, {"papers": [_paper()]})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Body parsing & validation
# ---------------------------------------------------------------------------

def test_missing_papers_list_returns_400(client):
    resp = _post_json(client, {})
    assert resp.status_code == 400


def test_non_dict_body_returns_400(client):
    resp = client.post(
        "/api/v1/papers",
        data=json.dumps([_paper()]),  # list at top level, not an object
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert resp.status_code == 400


def test_missing_required_field_is_per_entry_error(client):
    """The whole request stays 200; the specific paper has status=error."""
    body = {"papers": [_paper(), _paper(slug="broken", abstract="")]}
    resp = _post_json(client, body)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["summary"]["ok"] == 1
    assert data["summary"]["failed"] == 1
    results_by_slug = {r.get("slug"): r for r in data["results"]}
    assert results_by_slug["merger-trees-project4"]["status"] == "ok"
    assert results_by_slug["broken"]["status"] == "error"
    assert "abstract" in results_by_slug["broken"]["error"]


def test_pdf_is_required(client):
    resp = _post_json(client, {"papers": [_paper(pdf=None)]})
    data = resp.get_json()
    assert data["summary"]["ok"] == 0
    assert "pdf" in data["results"][0]["error"].lower()


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def test_single_paper_inline_pdf_is_ingested(app, client):
    resp = _post_json(client, {"papers": [_paper()]})
    assert resp.status_code == 200
    data = resp.get_json()
    result = data["results"][0]
    assert result["status"] == "ok"
    assert result["px_id"].startswith("2508.")
    assert result["action"] == "new"
    assert result["version"] == 1

    # Row landed with the right source_org + repo (slug)
    with app.app_context():
        conn = db_service.get_db()
        row = conn.execute(
            "SELECT source_org, repo, author FROM papers WHERE px_id = ?",
            (result["px_id"],),
        ).fetchone()
        assert row["source_org"] == ORG
        assert row["repo"] == "merger-trees-project4"
        assert row["author"] == "Denario-0"


def test_idempotent_resubmission_returns_unchanged(client):
    _post_json(client, {"papers": [_paper()]})
    resp = _post_json(client, {"papers": [_paper()]})
    result = resp.get_json()["results"][0]
    assert result["action"] == "unchanged"
    assert result["version"] == 1


def test_content_change_bumps_version(client):
    _post_json(client, {"papers": [_paper()]})
    resp = _post_json(client, {"papers": [_paper(abstract="A revised abstract after review.")]})
    result = resp.get_json()["results"][0]
    assert result["action"] == "updated"
    assert result["version"] == 2


def test_bib_is_parsed_and_cited(app, client):
    bib = """
@misc{wang2020,
  title={Concentrations of Dark Haloes Emerge from Their Merger Histories},
  author={Wang, Kuan},
  year={2020},
  eprint={2004.13732},
  archivePrefix={arXiv},
}
@misc{okoli2017,
  title={Dark matter halo concentrations},
  author={Okoli, Chiamaka},
  year={2017},
  eprint={1711.05277},
  archivePrefix={arXiv},
}
"""
    body = {"papers": [_paper(bib={"base64": base64.b64encode(bib.encode()).decode()})]}
    resp = _post_json(client, body)
    assert resp.status_code == 200
    result = resp.get_json()["results"][0]
    assert result["citations_count"] == 2

    with app.app_context():
        conn = db_service.get_db()
        rows = conn.execute(
            "SELECT citation_key, arxiv_id FROM citations WHERE citing_px_id = ? ORDER BY citation_key",
            (result["px_id"],),
        ).fetchall()
        assert [r["citation_key"] for r in rows] == ["okoli2017", "wang2020"]
        assert {r["arxiv_id"] for r in rows} == {"2004.13732", "1711.05277"}


def test_three_paper_batch_with_partial_failure(app, client):
    """Two valid, one malformed: summary = 2 ok / 1 failed, DB reflects the two."""
    body = {"papers": [
        _paper(slug="paper-a"),
        _paper(slug="paper-b", title="Paper B"),
        _paper(slug="paper-c", date=""),  # missing required field
    ]}
    resp = _post_json(client, body)
    data = resp.get_json()
    assert data["summary"]["total"] == 3
    assert data["summary"]["ok"] == 2
    assert data["summary"]["failed"] == 1

    with app.app_context():
        conn = db_service.get_db()
        slugs = [
            r["repo"] for r in conn.execute(
                "SELECT repo FROM papers WHERE source_org = ? ORDER BY repo", (ORG,)
            )
        ]
    assert slugs == ["paper-a", "paper-b"]


# ---------------------------------------------------------------------------
# Multipart
# ---------------------------------------------------------------------------

def test_multipart_inline_upload(app, client):
    manifest = {
        "papers": [
            {k: v for k, v in _paper(slug="mp-paper").items() if k != "pdf"},
        ]
    }
    data = {
        "manifest": json.dumps(manifest),
        "pdf_mp-paper": (io.BytesIO(b"%PDF-1.5\n%mp\n"), "paper.pdf", "application/pdf"),
    }
    resp = client.post(
        "/api/v1/papers",
        data=data,
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert resp.status_code == 200
    result = resp.get_json()["results"][0]
    assert result["status"] == "ok"
    assert result["action"] == "new"

    with app.app_context():
        conn = db_service.get_db()
        row = conn.execute(
            "SELECT repo, source_org FROM papers WHERE px_id = ?", (result["px_id"],),
        ).fetchone()
        assert row["repo"] == "mp-paper"
        assert row["source_org"] == ORG


def test_unsupported_content_type_returns_415(client):
    resp = client.post(
        "/api/v1/papers",
        data="body",
        headers={
            "Content-Type": "text/plain",
            "Authorization": f"Bearer {VALID_TOKEN}",
        },
    )
    assert resp.status_code == 415
