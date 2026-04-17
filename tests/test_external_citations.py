"""Tests for the external arXiv → Parallel ArXiv citation scraper.

The scraper itself is covered without live network access: the urllib
``url_opener`` is dependency-injected, so ``process_day`` can be driven
by a tiny fake that returns a canned Atom listing plus a fabricated tar.gz.
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import tarfile
from pathlib import Path

import pytest

from browse.services import database as db_service
from browse.services import external_citations as ec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "papers.db"
    monkeypatch.setattr(db_service, "_DB_PATH", "")
    db_service.init_standalone(str(db_path))
    conn = db_service.get_db_standalone()
    yield conn
    conn.close()


def _make_tarball(files: dict[str, str]) -> bytes:
    """Build a tar.gz blob of {filename: content} for extract_bib_and_tex tests."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# extract_bib_and_tex
# ---------------------------------------------------------------------------

class TestExtractBibAndTex:
    def test_empty_blob_returns_empty_strings(self):
        assert ec.extract_bib_and_tex(b"") == ("", "")

    def test_targz_with_mixed_files(self):
        blob = _make_tarball({
            "paper.tex": r"\documentclass{article} \cite{smith}",
            "other.tex": r"\input{paper}",
            "refs.bib": "@article{smith,\n  title = {x},\n}\n",
            "README.md": "ignore me",  # non-tex/bib should be dropped
        })
        bib, tex = ec.extract_bib_and_tex(blob)
        assert "smith" in bib
        assert r"\documentclass" in tex
        assert r"\input{paper}" in tex
        assert "ignore me" not in tex
        assert "ignore me" not in bib

    def test_bbl_is_treated_as_tex(self):
        # .bbl (compiled bibliography) is a common alternative to .bib and
        # contains the same PX references we want to match.
        blob = _make_tarball({
            "paper.bbl": r"\bibitem{foo} cites \href{https://papers.parallelscience.org/abs/2604.00017}{PX paper}",
        })
        _, tex = ec.extract_bib_and_tex(blob)
        assert "2604.00017" in tex

    def test_gzipped_single_tex(self):
        content = r"\documentclass{article} PX:2604.00001"
        blob = gzip.compress(content.encode("utf-8"))
        bib, tex = ec.extract_bib_and_tex(blob)
        assert bib == ""
        assert "PX:2604.00001" in tex

    def test_raw_text_fallback(self):
        # Not a tarball, not gzip — treated as text
        blob = b"plain text source with PX:2604.00042 inside"
        bib, tex = ec.extract_bib_and_tex(blob)
        assert bib == ""
        assert "PX:2604.00042" in tex


# ---------------------------------------------------------------------------
# find_px_citations — each of the three bib match modes + tex regex
# ---------------------------------------------------------------------------

class TestFindPxCitations:
    def test_bib_archive_prefix_match(self):
        # NOTE: the bib parser in citations.py requires the closing '}' to sit
        # at column 0 on its own line (regex `\n\}`), so these fixtures are
        # dedented deliberately.
        bib = (
            "@article{foo,\n"
            "  title = {A paper},\n"
            "  eprint = {2604.00017v1},\n"
            "  archivePrefix = {ParallelArxiv},\n"
            "}\n"
        )
        hits = ec.find_px_citations(bib, "", conn=None)
        assert hits == [{"cited_px_id": "2604.00017", "match_method": "bib_archive_prefix"}]

    def test_bib_citation_key_match(self):
        bib = (
            "@article{PX:2604.00017,\n"
            "  title = {Something},\n"
            "  author = {X},\n"
            "}\n"
        )
        hits = ec.find_px_citations(bib, "", conn=None)
        assert hits == [{"cited_px_id": "2604.00017", "match_method": "bib_citation_key"}]

    def test_bib_url_match(self):
        bib = (
            "@misc{foo,\n"
            "  title = {Random},\n"
            "  url = {https://papers.parallelscience.org/abs/2604.00042},\n"
            "}\n"
        )
        hits = ec.find_px_citations(bib, "", conn=None)
        assert hits == [{"cited_px_id": "2604.00042", "match_method": "bib_url"}]

    def test_tex_regex_fallback(self):
        tex = r"See \href{https://papers.parallelscience.org/abs/2604.00099}{this paper} and also PX:2604.00100."
        hits = ec.find_px_citations("", tex, conn=None)
        cited = {h["cited_px_id"] for h in hits}
        assert cited == {"2604.00099", "2604.00100"}
        assert all(h["match_method"] == "tex_regex" for h in hits)

    def test_bib_match_takes_precedence_over_tex(self):
        bib = (
            "@article{PX:2604.00017,\n"
            "  title = {x},\n"
            "}\n"
        )
        tex = r"also mentioned inline as PX:2604.00017"
        hits = ec.find_px_citations(bib, tex, conn=None)
        assert len(hits) == 1
        assert hits[0] == {"cited_px_id": "2604.00017", "match_method": "bib_citation_key"}

    def test_non_px_entries_are_ignored(self):
        bib = (
            "@article{einstein1905,\n"
            "  title = {On electrodynamics},\n"
            "  eprint = {physics/0102009},\n"
            "  archivePrefix = {arXiv},\n"
            "}\n"
        )
        assert ec.find_px_citations(bib, "", conn=None) == []

    def test_empty_inputs(self):
        assert ec.find_px_citations("", "", conn=None) == []


# ---------------------------------------------------------------------------
# upsert_external_citations — idempotency + metadata refresh
# ---------------------------------------------------------------------------

class TestUpsertExternalCitations:
    def test_inserts_one_row_per_hit(self, fresh_db):
        hits = [
            {"cited_px_id": "2604.00001", "match_method": "bib_citation_key"},
            {"cited_px_id": "2604.00002", "match_method": "bib_url"},
        ]
        n = ec.upsert_external_citations(
            fresh_db, hits,
            source="arxiv", external_id="2604.12345", external_version="v1",
            title="Ext paper", authors="A. Alice", year="2026",
            posted_date="2026-04-15T10:00:00Z",
        )
        assert n == 2

        rows = fresh_db.execute(
            "SELECT * FROM external_citations ORDER BY cited_px_id"
        ).fetchall()
        assert [r["cited_px_id"] for r in rows] == ["2604.00001", "2604.00002"]
        assert rows[0]["external_id"] == "2604.12345"
        assert rows[0]["external_version"] == "v1"
        assert rows[0]["title"] == "Ext paper"
        assert rows[0]["match_method"] == "bib_citation_key"

    def test_rerun_updates_metadata_without_duplicating(self, fresh_db):
        hits = [{"cited_px_id": "2604.00001", "match_method": "bib_citation_key"}]
        ec.upsert_external_citations(
            fresh_db, hits,
            source="arxiv", external_id="2604.12345", external_version="v1",
            title="Old title", authors="A", year="2026",
            posted_date="2026-04-15T10:00:00Z",
        )
        # Second call — new version of the same arXiv paper.
        ec.upsert_external_citations(
            fresh_db, hits,
            source="arxiv", external_id="2604.12345", external_version="v2",
            title="New title", authors="A, B", year="2026",
            posted_date="2026-04-20T10:00:00Z",
        )
        rows = fresh_db.execute(
            "SELECT * FROM external_citations WHERE external_id = '2604.12345'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["external_version"] == "v2"
        assert rows[0]["title"] == "New title"
        assert rows[0]["authors"] == "A, B"

    def test_empty_hits_is_noop(self, fresh_db):
        n = ec.upsert_external_citations(
            fresh_db, [],
            source="arxiv", external_id="2604.12345", external_version="v1",
            title=None, authors=None, year=None, posted_date=None,
        )
        assert n == 0
        count = fresh_db.execute("SELECT COUNT(*) FROM external_citations").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

class TestReadHelpers:
    def test_get_external_cited_by_orders_by_posted_date_desc(self, fresh_db):
        hits = [{"cited_px_id": "2604.00001", "match_method": "bib_citation_key"}]
        ec.upsert_external_citations(
            fresh_db, hits, source="arxiv", external_id="2604.11111",
            external_version="v1", title="Older", authors=None, year="2026",
            posted_date="2026-04-01T10:00:00Z",
        )
        ec.upsert_external_citations(
            fresh_db, hits, source="arxiv", external_id="2604.22222",
            external_version="v1", title="Newer", authors=None, year="2026",
            posted_date="2026-04-15T10:00:00Z",
        )
        results = ec.get_external_cited_by(fresh_db, "2604.00001")
        assert [r["external_id"] for r in results] == ["2604.22222", "2604.11111"]

    def test_cited_by_count(self, fresh_db):
        ec.upsert_external_citations(
            fresh_db,
            [{"cited_px_id": "2604.00001", "match_method": "bib_citation_key"}],
            source="arxiv", external_id="2604.11111", external_version="v1",
            title=None, authors=None, year=None, posted_date=None,
        )
        ec.upsert_external_citations(
            fresh_db,
            [{"cited_px_id": "2604.00001", "match_method": "bib_citation_key"}],
            source="arxiv", external_id="2604.22222", external_version="v1",
            title=None, authors=None, year=None, posted_date=None,
        )
        assert ec.get_external_cited_by_count(fresh_db, "2604.00001") == 2
        assert ec.get_external_cited_by_count(fresh_db, "2604.99999") == 0


# ---------------------------------------------------------------------------
# State round-trip
# ---------------------------------------------------------------------------

class TestScraperState:
    def test_get_and_set_state(self, fresh_db):
        assert ec.get_external_state(fresh_db, "arxiv") is None
        ec.set_external_state(fresh_db, "arxiv", dt.date(2026, 4, 15))
        assert ec.get_external_state(fresh_db, "arxiv") == "2026-04-15"
        # Overwrite works
        ec.set_external_state(fresh_db, "arxiv", dt.date(2026, 4, 16))
        assert ec.get_external_state(fresh_db, "arxiv") == "2026-04-16"


# ---------------------------------------------------------------------------
# Atom feed parsing
# ---------------------------------------------------------------------------

_SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2604.12345v2</id>
    <title>A neat
         paper   title</title>
    <published>2026-04-15T10:00:00Z</published>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2604.99999v1</id>
    <title>Another</title>
    <published>2026-04-15T11:00:00Z</published>
    <author><name>Carol</name></author>
  </entry>
</feed>
"""


class TestAtomFeedParsing:
    def test_parse_basic_feed(self):
        papers = ec._parse_atom_feed(_SAMPLE_FEED)
        assert len(papers) == 2
        assert papers[0].arxiv_id == "2604.12345"
        assert papers[0].version == "v2"
        # Whitespace/newlines collapsed
        assert papers[0].title == "A neat paper title"
        assert papers[0].authors == "Alice, Bob"
        assert papers[1].version == "v1"

    def test_malformed_feed_returns_empty(self):
        assert ec._parse_atom_feed("<not xml") == []


# ---------------------------------------------------------------------------
# process_day — end-to-end with a fake url_opener
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, n: int | None = None) -> bytes:
        if n is None:
            return self._body
        return self._body[: n]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _make_fake_opener(routes: dict[str, bytes]):
    """Route URLs (substring match) to canned response bytes."""

    def opener(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        for needle, body in routes.items():
            if needle in url:
                return _FakeResponse(body)
        raise AssertionError(f"unexpected URL in test: {url}")

    return opener


class TestProcessDay:
    def test_finds_and_upserts_citation_from_fake_arxiv(self, fresh_db, monkeypatch):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2604.12345v1</id>
    <title>External paper</title>
    <published>2026-04-15T10:00:00Z</published>
    <author><name>A. Alice</name></author>
  </entry>
</feed>
""".encode("utf-8")
        # Tarball containing a .bib that cites PX:2604.00017
        source_blob = _make_tarball({
            "refs.bib": "@article{PX:2604.00017,\n  title = {PX paper},\n  author = {X},\n  year = {2026},\n}\n",
            "main.tex": r"\cite{PX:2604.00017}",
        })
        opener = _make_fake_opener({
            "export.arxiv.org/api/query": feed,
            "export.arxiv.org/e-print/2604.12345": source_blob,
        })

        stats = ec.process_day(
            fresh_db, dt.date(2026, 4, 15),
            sleep=0, url_opener=opener,
        )
        assert stats["papers_scanned"] == 1
        assert stats["sources_fetched"] == 1
        assert stats["px_hits"] == 1
        assert stats["citing_papers"] == 1

        row = fresh_db.execute(
            "SELECT * FROM external_citations"
        ).fetchone()
        assert row["cited_px_id"] == "2604.00017"
        assert row["external_id"] == "2604.12345"
        assert row["external_version"] == "v1"
        assert row["title"] == "External paper"
        assert row["authors"] == "A. Alice"
        assert row["year"] == "2026"
        assert row["match_method"] == "bib_citation_key"
        assert row["posted_date"] == "2026-04-15T10:00:00Z"

    def test_dry_run_does_not_write(self, fresh_db):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2604.12345v1</id>
    <title>X</title>
    <published>2026-04-15T10:00:00Z</published>
    <author><name>A</name></author>
  </entry>
</feed>
""".encode("utf-8")
        source_blob = _make_tarball({"refs.bib": "@article{PX:2604.00017,\n  title = {x},\n}\n"})
        opener = _make_fake_opener({
            "export.arxiv.org/api/query": feed,
            "export.arxiv.org/e-print/": source_blob,
        })

        stats = ec.process_day(
            fresh_db, dt.date(2026, 4, 15),
            sleep=0, dry_run=True, url_opener=opener,
        )
        assert stats["px_hits"] == 1
        assert fresh_db.execute("SELECT COUNT(*) FROM external_citations").fetchone()[0] == 0

    def test_skips_papers_with_no_px_matches(self, fresh_db):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2604.00001v1</id>
    <title>unrelated paper</title>
    <published>2026-04-15T10:00:00Z</published>
    <author><name>A</name></author>
  </entry>
</feed>
""".encode("utf-8")
        source_blob = _make_tarball({
            "refs.bib": "@article{einstein1905, title={nothing here}}",
        })
        opener = _make_fake_opener({
            "export.arxiv.org/api/query": feed,
            "export.arxiv.org/e-print/": source_blob,
        })
        stats = ec.process_day(fresh_db, dt.date(2026, 4, 15), sleep=0, url_opener=opener)
        assert stats["papers_scanned"] == 1
        assert stats["px_hits"] == 0
        assert fresh_db.execute("SELECT COUNT(*) FROM external_citations").fetchone()[0] == 0
