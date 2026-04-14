"""Tests for citation ingestion, focused on duplicate-key behavior.

Auto-generated bibliographies frequently emit the same ``@misc{key, ...}``
multiple times. The citations table is keyed on
``(citing_px_id, citation_key)``, so duplicates collapse into a single row —
``upsert_citations`` must return the number of distinct keys written, not
the number of entries it tried to insert.
"""

from pathlib import Path

import pytest

from browse.services import database as db_service
from browse.services.citations import upsert_citations


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "papers.db"
    monkeypatch.setattr(db_service, "_DB_PATH", "")
    db_service.init_standalone(str(db_path))
    conn = db_service.get_db_standalone()
    yield conn
    conn.close()


def _entry(key: str, title: str = "", eprint: str = "") -> dict:
    return {
        "citation_key": key,
        "entry_type": "misc",
        "title": title,
        "author": "",
        "year": "",
        "doi": "",
        "eprint": eprint,
        "archive_prefix": "arxiv" if eprint else "",
        "url": "",
    }


def test_upsert_citations_counts_distinct_keys(fresh_db):
    """Duplicate citation keys collapse — count reflects distinct rows."""
    entries = [
        _entry("smith2020"),
        _entry("jones2021"),
        _entry("smith2020"),  # duplicate
        _entry("smith2020"),  # duplicate
        _entry("wang2023"),
    ]
    count = upsert_citations(fresh_db, "2604.00001", entries)

    assert count == 3

    rows = fresh_db.execute(
        "SELECT citation_key FROM citations WHERE citing_px_id = ? ORDER BY citation_key",
        ("2604.00001",),
    ).fetchall()
    assert [r["citation_key"] for r in rows] == ["jones2021", "smith2020", "wang2023"]


def test_upsert_citations_last_duplicate_wins(fresh_db):
    """When a key repeats with different fields, the last occurrence wins —
    matching the prior INSERT OR REPLACE semantics."""
    entries = [
        _entry("wang2020", title="Early draft title", eprint="2001.11111"),
        _entry("wang2020", title="Final title",       eprint="2004.13732"),
    ]
    count = upsert_citations(fresh_db, "2604.00001", entries)

    assert count == 1
    row = fresh_db.execute(
        "SELECT title, arxiv_id FROM citations WHERE citing_px_id = ? AND citation_key = ?",
        ("2604.00001", "wang2020"),
    ).fetchone()
    assert row["title"] == "Final title"
    assert row["arxiv_id"] == "2004.13732"


def test_upsert_citations_replaces_on_rerun(fresh_db):
    """A second call for the same citing paper replaces all prior rows."""
    upsert_citations(fresh_db, "2604.00001", [_entry("a"), _entry("b"), _entry("c")])
    assert fresh_db.execute(
        "SELECT COUNT(*) AS n FROM citations WHERE citing_px_id = ?", ("2604.00001",)
    ).fetchone()["n"] == 3

    # Rerun with a different set: old rows are cleared before new ones land.
    count = upsert_citations(fresh_db, "2604.00001", [_entry("x"), _entry("y")])
    assert count == 2
    keys = [
        r["citation_key"]
        for r in fresh_db.execute(
            "SELECT citation_key FROM citations WHERE citing_px_id = ? ORDER BY citation_key",
            ("2604.00001",),
        )
    ]
    assert keys == ["x", "y"]
