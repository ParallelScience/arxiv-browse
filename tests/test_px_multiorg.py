"""Tests for multi-org paper submission support.

Covers the (org, repo) keyed id_registry, v0→v1 DB migration, per-org
webhook signature validation, and the APPROVED_ORGS allowlist.

These tests do not depend on the heavy legacy-arxiv conftest fixtures —
they drive the PX tables directly via ``init_standalone`` and exercise
the webhook with a minimal Flask app so they can run in isolation.
"""

import hashlib
import hmac
import json
import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from browse.routes.webhook import blueprint as webhook_bp
from browse.services import database as db_service
from browse.services.id_registry import get_id_for_repo, get_or_assign_id
from browse.services.scraper import upsert_paper


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch):
    """A freshly-created v1 database; yields the connection."""
    db_path = tmp_path / "papers.db"
    # Reset module state so parallel tests don't leak paths
    monkeypatch.setattr(db_service, "_DB_PATH", "")
    db_service.init_standalone(str(db_path))
    conn = db_service.get_db_standalone()
    yield conn
    conn.close()


def _fake_meta(repo: str, date: str = "2026-04-05") -> dict:
    return {
        "repo": repo,
        "title": f"A Paper from {repo}",
        "author": "Alice Example, Bob Example",
        "date": date,
        "abstract": "We study a thing and report findings.",
        "primary_category": "astro-ph.CO",
        "secondary_categories": ["cs.LG"],
        "pages_url": f"https://example.github.io/{repo}/",
        "github_url": f"https://github.com/ExampleOrg/{repo}",
        "pdf_source_url": f"https://example.github.io/{repo}/paper.pdf",
    }


# ---------------------------------------------------------------------------
# id_registry
# ---------------------------------------------------------------------------

def test_same_repo_name_across_orgs_gets_distinct_ids(fresh_db):
    """Two orgs owning same-named repos must receive different PX IDs."""
    id_a = get_or_assign_id(fresh_db, "ParallelScience", "widgets", "2026-04-05")
    id_b = get_or_assign_id(fresh_db, "AcmeLabs", "widgets", "2026-04-05")

    assert id_a != id_b
    # Both drawn from the same shared pool (April 2026): consecutive numbers.
    assert id_a.startswith("2604.")
    assert id_b.startswith("2604.")

    # Lookups are org-scoped.
    assert get_id_for_repo(fresh_db, "ParallelScience", "widgets") == id_a
    assert get_id_for_repo(fresh_db, "AcmeLabs", "widgets") == id_b
    assert get_id_for_repo(fresh_db, "AcmeLabs", "does-not-exist") is None


def test_get_or_assign_id_is_stable(fresh_db):
    """Calling twice for the same (org, repo) returns the same ID."""
    first = get_or_assign_id(fresh_db, "AcmeLabs", "neutrino-paper", "2026-04-05")
    second = get_or_assign_id(fresh_db, "AcmeLabs", "neutrino-paper", "2026-04-05")
    assert first == second


def test_upsert_paper_records_source_org(fresh_db):
    """source_org is persisted on papers rows for provenance tracking."""
    meta = _fake_meta("widgets")
    px_id, version, action = upsert_paper(
        fresh_db, meta, "AcmeLabs",
        local_pdf_dir=None, gcs_bucket=None, skip_pdf=True,
    )
    assert action == "new"
    assert version == 1

    row = fresh_db.execute(
        "SELECT source_org, repo FROM papers WHERE px_id = ? AND is_current = 1",
        (px_id,),
    ).fetchone()
    assert row["source_org"] == "AcmeLabs"
    assert row["repo"] == "widgets"


# ---------------------------------------------------------------------------
# v0 → v1 migration
# ---------------------------------------------------------------------------

# Schema the production DB had before multi-org support. Kept inline so the
# migration test can build a v0 fixture without resurrecting old code.
_V0_SCHEMA = """
CREATE TABLE id_registry (
    repo        TEXT PRIMARY KEY,
    px_id       TEXT NOT NULL UNIQUE,
    yymm        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE id_sequence (
    yymm    TEXT PRIMARY KEY,
    next_n  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE papers (
    px_id               TEXT NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1,
    title               TEXT NOT NULL,
    author              TEXT NOT NULL,
    date                TEXT NOT NULL,
    abstract            TEXT NOT NULL,
    primary_category    TEXT NOT NULL,
    secondary_categories TEXT NOT NULL DEFAULT '[]',
    repo                TEXT NOT NULL,
    pages_url           TEXT NOT NULL,
    github_url          TEXT NOT NULL,
    pdf_url             TEXT NOT NULL,
    is_current          INTEGER NOT NULL DEFAULT 1,
    scraped_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    content_hash        TEXT,
    PRIMARY KEY (px_id, version),
    FOREIGN KEY (repo) REFERENCES id_registry(repo)
);
"""


def test_migration_v0_to_v1_preserves_data(tmp_path: Path, monkeypatch):
    """A legacy single-org DB migrates cleanly: rows survive, org columns
    default to 'ParallelScience', PX IDs are unchanged, user_version = 1."""
    db_path = tmp_path / "legacy.db"

    # Build a v0 DB with a few rows, matching the pre-migration shape.
    conn = sqlite3.connect(db_path)
    conn.executescript(_V0_SCHEMA)
    conn.execute(
        "INSERT INTO id_registry (repo, px_id, yymm) VALUES (?, ?, ?)",
        ("legacy-paper", "2604.00001", "2604"),
    )
    conn.execute(
        "INSERT INTO id_sequence (yymm, next_n) VALUES (?, ?)", ("2604", 2)
    )
    conn.execute(
        "INSERT INTO papers (px_id, version, title, author, date, abstract, "
        "primary_category, repo, pages_url, github_url, pdf_url, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2604.00001", 1, "Legacy title", "Legacy Author", "2026-04-01",
            "Legacy abstract", "astro-ph.CO", "legacy-paper",
            "https://parallelscience.github.io/legacy-paper/",
            "https://github.com/ParallelScience/legacy-paper",
            "https://storage.googleapis.com/parallel-arxiv-pdfs/2604.00001v1.pdf",
            "deadbeef",
        ),
    )
    conn.commit()
    conn.close()

    # Run the migration by initializing the DB through the service layer.
    monkeypatch.setattr(db_service, "_DB_PATH", "")
    db_service.init_standalone(str(db_path))
    conn = db_service.get_db_standalone()

    try:
        # user_version bumped
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

        # id_registry row preserved and backfilled
        reg = conn.execute(
            "SELECT org, repo, px_id FROM id_registry"
        ).fetchall()
        assert len(reg) == 1
        assert reg[0]["org"] == "ParallelScience"
        assert reg[0]["repo"] == "legacy-paper"
        assert reg[0]["px_id"] == "2604.00001"

        # papers row preserved with source_org backfill
        papers = conn.execute(
            "SELECT source_org, repo, title, px_id FROM papers"
        ).fetchall()
        assert len(papers) == 1
        assert papers[0]["source_org"] == "ParallelScience"
        assert papers[0]["title"] == "Legacy title"
        assert papers[0]["px_id"] == "2604.00001"

        # id_sequence untouched
        seq = conn.execute(
            "SELECT yymm, next_n FROM id_sequence"
        ).fetchone()
        assert seq["yymm"] == "2604"
        assert seq["next_n"] == 2
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path: Path, monkeypatch):
    """Running init on an already-migrated DB is a no-op."""
    db_path = tmp_path / "papers.db"
    monkeypatch.setattr(db_service, "_DB_PATH", "")
    db_service.init_standalone(str(db_path))

    # Re-init: should not error, should not change user_version.
    db_service.init_standalone(str(db_path))
    conn = db_service.get_db_standalone()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Webhook auth
# ---------------------------------------------------------------------------

def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _page_build_payload(org: str, repo: str, built: bool = True) -> bytes:
    return json.dumps({
        "build": {"status": "built" if built else "errored"},
        "repository": {"name": repo},
        "organization": {"login": org},
    }).encode()


@pytest.fixture
def webhook_app(tmp_path, monkeypatch):
    """Minimal Flask app wired to the webhook blueprint + initialized PX DB."""
    db_path = tmp_path / "papers.db"
    monkeypatch.setattr(db_service, "_DB_PATH", "")
    db_service.init_standalone(str(db_path))

    app = Flask(__name__)
    app.config["APPROVED_ORGS"] = ["ParallelScience", "AcmeLabs"]
    app.config["PX_DATABASE_PATH"] = str(db_path)
    app.config["GCS_BUCKET"] = ""
    app.config["PDF_LOCAL_DIR"] = ""
    app.register_blueprint(webhook_bp)
    # Route get_db() through the same standalone path
    db_service._DB_PATH = str(db_path)
    app.teardown_appcontext(db_service.close_db)
    return app


def test_webhook_rejects_unapproved_org(webhook_app, monkeypatch):
    """A page_build from an org not in APPROVED_ORGS gets 403."""
    monkeypatch.setenv("WEBHOOK_SECRET_EVILORG", "badsecret")
    body = _page_build_payload("EvilOrg", "malicious-repo")

    client = webhook_app.test_client()
    resp = client.post(
        "/webhook/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "page_build",
            "X-Hub-Signature-256": _sign("badsecret", body),
        },
    )
    assert resp.status_code == 403


def test_webhook_rejects_missing_secret(webhook_app, monkeypatch):
    """Approved org with no configured secret is still rejected."""
    # Ensure there is no WEBHOOK_SECRET_ACMELABS set.
    monkeypatch.delenv("WEBHOOK_SECRET_ACMELABS", raising=False)
    body = _page_build_payload("AcmeLabs", "paper")

    client = webhook_app.test_client()
    resp = client.post(
        "/webhook/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "page_build",
            "X-Hub-Signature-256": _sign("guessed", body),
        },
    )
    assert resp.status_code == 403


def test_webhook_rejects_wrong_signature(webhook_app, monkeypatch):
    """Approved org, right secret name, wrong HMAC → 403."""
    monkeypatch.setenv("WEBHOOK_SECRET_ACMELABS", "real-secret")
    body = _page_build_payload("AcmeLabs", "paper")

    client = webhook_app.test_client()
    resp = client.post(
        "/webhook/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "page_build",
            "X-Hub-Signature-256": _sign("wrong-secret", body),
        },
    )
    assert resp.status_code == 403


def test_webhook_cross_org_signature_forgery_rejected(webhook_app, monkeypatch):
    """Signing with ParallelScience's secret but claiming to be AcmeLabs
    must be rejected — each org's requests must use that org's secret."""
    monkeypatch.setenv("WEBHOOK_SECRET_PARALLELSCIENCE", "ps-secret")
    monkeypatch.setenv("WEBHOOK_SECRET_ACMELABS", "acme-secret")
    body = _page_build_payload("AcmeLabs", "paper")

    client = webhook_app.test_client()
    resp = client.post(
        "/webhook/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "page_build",
            "X-Hub-Signature-256": _sign("ps-secret", body),
        },
    )
    assert resp.status_code == 403


def test_webhook_accepts_valid_signed_page_build(webhook_app, monkeypatch):
    """An approved org with the correct signature gets past auth.

    The scraper is stubbed to return None so the handler returns 204
    "no paper metadata" — that's fine; we're only verifying the auth path.
    """
    monkeypatch.setenv("WEBHOOK_SECRET_ACMELABS", "acme-secret")
    # Stub scrape_single_repo to avoid hitting GitHub in tests.
    import browse.services.scraper as scraper_mod
    monkeypatch.setattr(scraper_mod, "scrape_single_repo", lambda org, repo: None)

    body = _page_build_payload("AcmeLabs", "paper")
    client = webhook_app.test_client()
    resp = client.post(
        "/webhook/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "page_build",
            "X-Hub-Signature-256": _sign("acme-secret", body),
        },
    )
    # 204 responses have no body per HTTP spec; status alone signals success.
    assert resp.status_code == 204


def test_webhook_ping_from_approved_org(webhook_app, monkeypatch):
    """Ping from an approved, correctly-signed org gets acknowledged."""
    monkeypatch.setenv("WEBHOOK_SECRET_ACMELABS", "acme-secret")
    body = json.dumps({"organization": {"login": "AcmeLabs"}, "zen": "..."}).encode()

    client = webhook_app.test_client()
    resp = client.post(
        "/webhook/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": _sign("acme-secret", body),
        },
    )
    assert resp.status_code == 200
    assert resp.data == b"pong"
