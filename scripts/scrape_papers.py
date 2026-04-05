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
    return {
        "title": parser.title,
        "author": parser.author,
        "date": parser.date,
        "abstract": parser.abstract,
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


def main():
    ap = argparse.ArgumentParser(description="Scrape ParallelScience papers")
    ap.add_argument("--org", default="ParallelScience", help="GitHub org name")
    ap.add_argument("--output", default="browse/data/papers.json", help="Output JSON path")
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

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(papers, f, indent=2)

    print(f"\nWrote {len(papers)} papers to {args.output}")


if __name__ == "__main__":
    main()
