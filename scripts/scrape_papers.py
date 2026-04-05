#!/usr/bin/env python3
"""Scrape ParallelScience GitHub Pages sites and upsert into the SQLite database.

Usage:
    python scripts/scrape_papers.py
    python scripts/scrape_papers.py --org ParallelScience --db browse/data/papers.db
    python scripts/scrape_papers.py --no-pdf
"""

import argparse
import os
import sys

# Ensure the project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browse.services.database import get_db_standalone, init_standalone
from browse.services.scraper import scrape_all_repos


def main():
    ap = argparse.ArgumentParser(description="Scrape ParallelScience papers")
    ap.add_argument("--org", default="ParallelScience", help="GitHub org name")
    ap.add_argument("--db", default="browse/data/papers.db", help="SQLite database path")
    ap.add_argument("--no-pdf", action="store_true", help="Skip PDF download")
    ap.add_argument("--pdf-dir", default="/rds/rds-ai-scientist/parallel-arxiv",
                    help="Local directory for PDFs")
    ap.add_argument("--gcs-bucket", default="parallel-arxiv-pdfs",
                    help="GCS bucket for PDFs")
    args = ap.parse_args()

    init_standalone(args.db)
    conn = get_db_standalone()

    try:
        counts = scrape_all_repos(
            conn, org=args.org,
            skip_pdf=args.no_pdf,
            local_pdf_dir=args.pdf_dir,
            gcs_bucket=args.gcs_bucket,
        )
    finally:
        conn.close()

    print(f"\nDone: {counts['new']} new, {counts['updated']} updated, "
          f"{counts['unchanged']} unchanged, {counts['failed']} failed")

    # Exit with code 1 if there were new or updated papers (used by deploy script)
    if counts["new"] + counts["updated"] > 0:
        sys.exit(0)
    sys.exit(0)


if __name__ == "__main__":
    main()
