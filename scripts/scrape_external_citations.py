#!/usr/bin/env python3
"""Scrape regular arXiv for papers that cite Parallel ArXiv papers.

Runs once per day (via cron), catches up from the last processed day through
yesterday (UTC). For each new arXiv paper it downloads the source tarball,
parses any ``.bib`` and ``.tex`` files, and records PX citations in
``external_citations``.

Usage::

    # Catch up from state table to yesterday (default daily cron mode):
    python scripts/scrape_external_citations.py --db browse/data/papers.db

    # Process a single day (e.g. for backfilling a missed run):
    python scripts/scrape_external_citations.py --db /tmp/papers.db --date 2026-04-15

    # Don't write to the DB (for verifying matching against a known day):
    python scripts/scrape_external_citations.py --db /tmp/papers.db --date 2026-04-15 --dry-run
"""

import argparse
import datetime as dt
import logging
import os
import sys

# Make the repo root importable when run as a script from scripts/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browse.services.database import get_db_standalone, init_standalone
from browse.services.external_citations import (
    get_external_state,
    process_day,
    set_external_state,
)

log = logging.getLogger(__name__)


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _days_to_process(conn, source: str, override: dt.date | None) -> list[dt.date]:
    if override is not None:
        return [override]
    yesterday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    last = get_external_state(conn, source)
    if last is None:
        # First run — start from yesterday only. Backfill is out of scope for
        # the MVP (see plan: "Going forward only").
        return [yesterday]
    last_date = dt.date.fromisoformat(last)
    start = last_date + dt.timedelta(days=1)
    if start > yesterday:
        return []
    days = []
    cur = start
    while cur <= yesterday:
        days.append(cur)
        cur += dt.timedelta(days=1)
    return days


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="browse/data/papers.db", help="SQLite database path")
    ap.add_argument("--source", default="arxiv", choices=["arxiv"],
                    help="External source to scrape (only 'arxiv' supported in MVP)")
    ap.add_argument("--date", type=_parse_date, default=None,
                    help="Process a single YYYY-MM-DD (UTC); overrides state-based catch-up")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't write to the DB; just log matches")
    ap.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    init_standalone(args.db)
    conn = get_db_standalone()

    try:
        days = _days_to_process(conn, args.source, args.date)
        if not days:
            log.info("nothing to do: already caught up through yesterday")
            return 0

        log.info("processing %d day(s): %s .. %s",
                 len(days), days[0].isoformat(), days[-1].isoformat())

        totals = {"papers_scanned": 0, "sources_fetched": 0,
                  "px_hits": 0, "citing_papers": 0}
        for day in days:
            stats = process_day(conn, day, source=args.source, dry_run=args.dry_run)
            log.info("day %s summary: %s", day.isoformat(), stats)
            for k, v in stats.items():
                totals[k] += v
            if not args.dry_run and args.date is None:
                # Only advance state for the auto catch-up path. Explicit
                # --date runs don't move the pointer, to keep them safely
                # replayable.
                set_external_state(conn, args.source, day)

        log.info("run totals: %s", totals)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
