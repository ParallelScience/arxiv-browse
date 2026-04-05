#!/usr/bin/env python3
"""One-time migration: import papers.json into the SQLite database.

Preserves existing PX IDs so that citations remain stable.

Usage:
    python scripts/migrate_json_to_sqlite.py
    python scripts/migrate_json_to_sqlite.py --json browse/data/papers.json --db browse/data/papers.db
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browse.services.database import get_db_standalone, init_standalone
from browse.services.scraper import compute_content_hash


def main():
    ap = argparse.ArgumentParser(description="Migrate papers.json to SQLite")
    ap.add_argument("--json", default="browse/data/papers.json", help="Source JSON")
    ap.add_argument("--db", default="browse/data/papers.db", help="Target SQLite DB")
    args = ap.parse_args()

    if not os.path.exists(args.json):
        print(f"Source file not found: {args.json}")
        sys.exit(1)

    with open(args.json) as f:
        papers = json.load(f)

    print(f"Loaded {len(papers)} papers from {args.json}")

    # Remove any existing DB to start fresh
    if os.path.exists(args.db):
        print(f"WARNING: {args.db} already exists. Will merge (skip existing IDs).")

    init_standalone(args.db)
    conn = get_db_standalone()

    try:
        for paper in papers:
            px_id = paper["px_id"]
            repo = paper["repo"]
            date = paper.get("date", "")

            # Derive YYMM
            match = re.match(r"(\d{4})-(\d{2})", date)
            yymm = (match.group(1)[2:] + match.group(2)) if match else "0000"

            # Insert into id_registry (skip if already exists)
            conn.execute(
                "INSERT OR IGNORE INTO id_registry (repo, px_id, yymm) VALUES (?, ?, ?)",
                (repo, px_id, yymm),
            )

            # Update id_sequence to be at least past this ID
            seq_match = re.match(r"\d{4}\.(\d+)", px_id)
            if seq_match:
                seq_num = int(seq_match.group(1))
                conn.execute(
                    "INSERT INTO id_sequence (yymm, next_n) VALUES (?, ?) "
                    "ON CONFLICT(yymm) DO UPDATE SET next_n = MAX(next_n, excluded.next_n)",
                    (yymm, seq_num + 1),
                )

            # Compute content hash
            content_hash = compute_content_hash(paper)

            # Ensure secondary_categories is JSON string
            sec_cats = paper.get("secondary_categories", [])
            if isinstance(sec_cats, list):
                sec_cats = json.dumps(sec_cats)

            # Insert paper as version 1
            conn.execute(
                "INSERT OR IGNORE INTO papers "
                "(px_id, version, title, author, date, abstract, "
                " primary_category, secondary_categories, repo, "
                " pages_url, github_url, pdf_url, is_current, content_hash) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    px_id,
                    paper.get("title", ""),
                    paper.get("author", ""),
                    date,
                    paper.get("abstract", ""),
                    paper.get("primary_category", ""),
                    sec_cats,
                    repo,
                    paper.get("pages_url", ""),
                    paper.get("github_url", ""),
                    paper.get("pdf_url", ""),
                    content_hash,
                ),
            )
            print(f"  {px_id} ({repo})")

        conn.commit()
        print(f"\nMigration complete. Database: {args.db}")

        # Summary
        count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        reg_count = conn.execute("SELECT COUNT(*) FROM id_registry").fetchone()[0]
        print(f"  {count} paper versions, {reg_count} registered IDs")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
