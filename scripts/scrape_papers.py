#!/usr/bin/env python3
"""Scrape ParallelScience GitHub Pages sites and build a paper index.

Queries the ParallelScience org for repos with GitHub Pages,
fetches each site's index.html, extracts metadata from the
template placeholders, and writes a JSON index.

Usage:
    python scripts/scrape_papers.py
    python scripts/scrape_papers.py --org ParallelScience --output browse/data/papers.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
from html.parser import HTMLParser


class PageParser(HTMLParser):
    """Extract title, author, date, abstract from a Denario GitHub Pages site."""

    def __init__(self):
        super().__init__()
        self._in_tag = None
        self._in_class = None
        self._depth = 0
        self.title = ""
        self.author = ""
        self.date = ""
        self.time = ""
        self.subject = ""
        self.abstract = ""
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")

        if tag == "h1" and self._in_class is None:
            self._in_tag = "title"
            self._current_text = []
        elif tag == "div" and "meta" in cls:
            self._in_class = "meta"
        elif tag == "span" and self._in_class == "meta":
            self._in_tag = "meta_span"
            self._current_text = []
        elif tag == "div" and "abstract" in cls:
            self._in_class = "abstract"
        elif tag == "p" and self._in_class == "abstract":
            self._in_tag = "abstract_p"
            self._current_text = []

    def handle_endtag(self, tag):
        if tag == "h1" and self._in_tag == "title":
            self.title = "".join(self._current_text).strip()
            self._in_tag = None
        elif tag == "span" and self._in_tag == "meta_span":
            text = "".join(self._current_text).strip()
            if text.startswith("Author:"):
                self.author = text.replace("Author:", "").strip()
            elif text.startswith("Date:"):
                self.date = text.replace("Date:", "").strip()
            elif text.startswith("Time:"):
                self.time = text.replace("Time:", "").strip()
            elif text.startswith("Subject:"):
                self.subject = text.replace("Subject:", "").strip()
            self._in_tag = None
        elif tag == "div" and self._in_class == "meta":
            self._in_class = None
        elif tag == "p" and self._in_tag == "abstract_p":
            self.abstract = "".join(self._current_text).strip()
            self._in_tag = None
        elif tag == "div" and self._in_class == "abstract":
            self._in_class = None

    def handle_data(self, data):
        if self._in_tag in ("title", "meta_span", "abstract_p"):
            self._current_text.append(data)


def list_repos(org: str) -> list[str]:
    """List repos in the org via GitHub API."""
    import urllib.request

    repos: list[str] = []
    page = 1
    while True:
        url = f"https://api.github.com/orgs/{org}/repos?per_page=100&page={page}"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        # Use GITHUB_TOKEN if available for higher rate limits
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        if not data:
            break
        repos.extend(r["name"] for r in data)
        page += 1
    return repos


def fetch_page(org: str, repo: str) -> str | None:
    """Fetch the GitHub Pages index.html for a repo."""
    url = f"https://{org.lower()}.github.io/{repo}/"
    result = subprocess.run(
        ["curl", "-sL", "--fail", "-o", "-", url],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def parse_page(html: str) -> dict | None:
    """Parse metadata from a Denario GitHub Pages site."""
    parser = PageParser()
    parser.feed(html)
    if not parser.title or not parser.abstract:
        return None
    # Parse subject into primary + secondary categories
    subject = parser.subject
    categories = [c.strip() for c in subject.split(";")] if subject else []
    primary_category = categories[0] if categories else ""
    secondary_categories = categories[1:] if len(categories) > 1 else []

    # Combine date and time if both present
    date = parser.date
    if parser.time:
        # Strip timezone suffix like "AOE" and combine
        time_clean = re.sub(r'\s*[A-Z]{2,}$', '', parser.time).strip()
        date = f"{date} {time_clean}"

    return {
        "title": parser.title,
        "author": parser.author,
        "date": date,
        "abstract": parser.abstract,
        "primary_category": primary_category,
        "secondary_categories": secondary_categories,
    }


def assign_ids(papers: list[dict]) -> None:
    """Assign PX IDs in YYMM.NNNNN format based on paper date."""
    from collections import defaultdict

    # Group by year-month
    by_month: defaultdict[str, list[dict]] = defaultdict(list)
    for paper in papers:
        date_str = paper.get("date", "")
        # Parse YYYY-MM-DD or YYYY-MM-DD HH:MM:SS
        match = re.match(r"(\d{4})-(\d{2})", date_str)
        if match:
            yymm = match.group(1)[2:] + match.group(2)  # e.g., "2604"
        else:
            yymm = "0000"
        by_month[yymm].append(paper)

    # Assign sequential IDs within each month
    for yymm, month_papers in by_month.items():
        # Sort by date for stable ordering
        month_papers.sort(key=lambda p: p.get("date", ""))
        for i, paper in enumerate(month_papers, start=1):
            paper["px_id"] = f"{yymm}.{i:05d}"


GCS_BUCKET = "parallel-arxiv-pdfs"
LOCAL_PDF_DIR = "/rds/rds-ai-scientist/parallel-arxiv"


def download_pdf(pdf_url: str, px_id: str, local_dir: str | None, gcs_bucket: str | None) -> bool:
    """Download a PDF and save locally and/or upload to GCS. Returns True on success."""
    result = subprocess.run(
        ["curl", "-sL", "--fail", "-o", "-", pdf_url],
        capture_output=True, timeout=30,
    )
    if result.returncode != 0 or not result.stdout:
        return False

    pdf_data = result.stdout
    filename = f"{px_id}.pdf"

    # Save locally
    if local_dir:
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, filename)
        with open(local_path, "wb") as f:
            f.write(pdf_data)

    # Upload to GCS
    if gcs_bucket:
        gcs_url = f"gs://{gcs_bucket}/{filename}"
        proc = subprocess.run(
            ["gsutil", "-q", "cp", "-", gcs_url],
            input=pdf_data, capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            print(f"GCS upload failed: {proc.stderr.decode()}", file=sys.stderr)
            return False

    return True


def main():
    ap = argparse.ArgumentParser(description="Scrape ParallelScience papers")
    ap.add_argument("--org", default="ParallelScience", help="GitHub org name")
    ap.add_argument("--output", default="browse/data/papers.json", help="Output JSON path")
    ap.add_argument("--no-pdf", action="store_true", help="Skip PDF download")
    ap.add_argument("--pdf-dir", default=LOCAL_PDF_DIR, help="Local directory for PDFs")
    ap.add_argument("--gcs-bucket", default=GCS_BUCKET, help="GCS bucket for PDFs")
    args = ap.parse_args()

    repos = list_repos(args.org)
    print(f"Found {len(repos)} repos in {args.org}")

    papers = []
    for repo in repos:
        print(f"  {repo}...", end=" ")
        html = fetch_page(args.org, repo)
        if not html:
            print("no pages site")
            continue
        meta = parse_page(html)
        if not meta:
            print("no paper metadata")
            continue
        meta["repo"] = repo
        meta["pages_url"] = f"https://{args.org.lower()}.github.io/{repo}/"
        meta["github_url"] = f"https://github.com/{args.org}/{repo}"
        meta["pdf_url"] = f"https://{args.org.lower()}.github.io/{repo}/paper.pdf"
        papers.append(meta)
        print(f"OK: {meta['title'][:60]}...")

    assign_ids(papers)

    # Download PDFs
    if not args.no_pdf:
        print("\n=== Downloading PDFs ===")
        for paper in papers:
            px_id = paper["px_id"]
            print(f"  {px_id}...", end=" ")
            ok = download_pdf(paper["pdf_url"], px_id, args.pdf_dir, args.gcs_bucket)
            if ok:
                paper["pdf_url"] = f"https://storage.googleapis.com/{args.gcs_bucket}/{px_id}.pdf"
                print("OK")
            else:
                print("FAILED")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(papers, f, indent=2)

    print(f"\nWrote {len(papers)} papers to {args.output}")


if __name__ == "__main__":
    main()
