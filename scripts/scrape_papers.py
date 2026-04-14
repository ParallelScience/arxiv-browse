#!/usr/bin/env python3
"""Scrape approved-org GitHub Pages sites and upsert into the SQLite database.

Usage:
    python scripts/scrape_papers.py
    python scripts/scrape_papers.py --orgs ParallelScience,AcmeLabs
    python scripts/scrape_papers.py --db browse/data/papers.db --no-pdf

The default org list is read from the ``APPROVED_ORGS`` env var
(comma-separated, same format as the Flask config), falling back to
``ParallelScience``. Pass ``--orgs`` to override explicitly.
"""

import argparse
import os
import sys

# Ensure the project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browse.services.database import get_db_standalone, init_standalone
from browse.services.scraper import scrape_all_repos


def _default_orgs() -> str:
    return os.environ.get("APPROVED_ORGS", "ParallelScience")


def main():
    ap = argparse.ArgumentParser(description="Scrape papers from approved GitHub orgs")
    ap.add_argument(
        "--orgs",
        default=_default_orgs(),
        help="Comma-separated list of GitHub org names (default: $APPROVED_ORGS or ParallelScience)",
    )
    ap.add_argument("--db", default="browse/data/papers.db", help="SQLite database path")
    ap.add_argument("--no-pdf", action="store_true", help="Skip PDF download")
    ap.add_argument("--pdf-dir", default="/rds/rds-ai-scientist/parallel-arxiv",
                    help="Local directory for PDFs")
    ap.add_argument("--gcs-bucket", default="parallel-arxiv-pdfs",
                    help="GCS bucket for PDFs")
    args = ap.parse_args()

    orgs = [o.strip() for o in args.orgs.split(",") if o.strip()]
    if not orgs:
        print("No orgs configured — set APPROVED_ORGS or pass --orgs", file=sys.stderr)
        sys.exit(2)

    init_standalone(args.db)
    conn = get_db_standalone()

    try:
        counts = scrape_all_repos(
            conn, orgs=orgs,
            skip_pdf=args.no_pdf,
            local_pdf_dir=args.pdf_dir,
            gcs_bucket=args.gcs_bucket,
        )
    finally:
        conn.close()

    print(f"\nDone: {counts['new']} new, {counts['updated']} updated, "
          f"{counts['unchanged']} unchanged, {counts['failed']} failed")


if __name__ == "__main__":
    main()
