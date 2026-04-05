"""Scraper service: fetch, parse, and upsert papers from GitHub Pages.

Used by both the webhook endpoint (single repo) and the CLI safety-net
scraper (all repos).
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from html.parser import HTMLParser

from browse.services.id_registry import get_or_assign_id

log = logging.getLogger(__name__)

GCS_BUCKET = "parallel-arxiv-pdfs"
LOCAL_PDF_DIR = "/rds/rds-ai-scientist/parallel-arxiv"


# ---------------------------------------------------------------------------
# HTML parser (moved from scripts/scrape_papers.py)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Fetching & parsing
# ---------------------------------------------------------------------------

def fetch_page(org: str, repo: str) -> str | None:
    """Fetch the GitHub Pages index.html for a repo."""
    import urllib.request

    url = f"https://{org.lower()}.github.io/{repo}/"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def parse_page(html: str) -> dict | None:
    """Parse metadata from a Denario GitHub Pages site."""
    parser = PageParser()
    parser.feed(html)
    if not parser.title or not parser.abstract:
        return None

    subject = parser.subject
    categories = [c.strip() for c in subject.split(";")] if subject else []
    primary_category = categories[0] if categories else ""
    secondary_categories = categories[1:] if len(categories) > 1 else []

    date = parser.date
    if parser.time:
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


def scrape_single_repo(org: str, repo: str) -> dict | None:
    """Fetch and parse a single repo's GitHub Pages site.

    Returns a metadata dict with repo/URL fields added, or None.
    """
    html = fetch_page(org, repo)
    if not html:
        return None
    meta = parse_page(html)
    if not meta:
        return None
    meta["repo"] = repo
    meta["pages_url"] = f"https://{org.lower()}.github.io/{repo}/"
    meta["github_url"] = f"https://github.com/{org}/{repo}"
    meta["pdf_source_url"] = f"https://{org.lower()}.github.io/{repo}/paper.pdf"
    return meta


# ---------------------------------------------------------------------------
# Content hashing (determines whether a version bump is needed)
# ---------------------------------------------------------------------------

def compute_content_hash(meta: dict) -> str:
    """Deterministic hash of content fields that matter for versioning."""
    payload = json.dumps({
        "title": meta["title"],
        "author": meta["author"],
        "abstract": meta["abstract"],
        "primary_category": meta["primary_category"],
        "secondary_categories": sorted(meta.get("secondary_categories", [])),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# PDF handling
# ---------------------------------------------------------------------------

def download_pdf(
    pdf_source_url: str,
    px_id: str,
    version: int,
    local_dir: str | None = None,
    gcs_bucket: str | None = None,
) -> str | None:
    """Download a PDF and store with versioned filename. Returns the public URL or None."""
    import urllib.request

    try:
        with urllib.request.urlopen(pdf_source_url, timeout=30) as resp:
            pdf_data = resp.read()
    except Exception as exc:
        log.error("PDF download failed for %s: %s", pdf_source_url, exc)
        return None

    if not pdf_data:
        return None

    filename = f"{px_id}v{version}.pdf"

    if local_dir:
        os.makedirs(local_dir, exist_ok=True)
        with open(os.path.join(local_dir, filename), "wb") as f:
            f.write(pdf_data)

    if gcs_bucket:
        try:
            from google.cloud import storage
            client = storage.Client()
            bucket = client.bucket(gcs_bucket)
            blob = bucket.blob(filename)
            blob.upload_from_string(pdf_data, content_type="application/pdf")
            return f"https://storage.googleapis.com/{gcs_bucket}/{filename}"
        except Exception as exc:
            log.error("GCS PDF upload failed: %s", exc)
            return None

    if local_dir:
        return os.path.join(local_dir, filename)
    return None


# ---------------------------------------------------------------------------
# Upsert logic
# ---------------------------------------------------------------------------

def upsert_paper(
    conn: sqlite3.Connection,
    meta: dict,
    org: str,
    local_pdf_dir: str | None = LOCAL_PDF_DIR,
    gcs_bucket: str | None = GCS_BUCKET,
    skip_pdf: bool = False,
) -> tuple[str, int, str]:
    """Insert or update a paper in the database.

    Returns (px_id, version, action) where action is "new", "updated", or "unchanged".
    """
    repo = meta["repo"]
    content_hash = compute_content_hash(meta)

    # Get or assign a stable PX ID
    px_id = get_or_assign_id(conn, repo, meta["date"])

    # Check if we already have this exact content
    existing = conn.execute(
        "SELECT version, content_hash FROM papers "
        "WHERE px_id = ? AND is_current = 1",
        (px_id,),
    ).fetchone()

    if existing and existing["content_hash"] == content_hash:
        return px_id, existing["version"], "unchanged"

    if existing:
        # Content changed — bump version
        old_version = existing["version"]
        new_version = old_version + 1
        conn.execute(
            "UPDATE papers SET is_current = 0 WHERE px_id = ? AND version = ?",
            (px_id, old_version),
        )
        action = "updated"
    else:
        new_version = 1
        action = "new"

    # Download PDF
    pdf_url = ""
    if not skip_pdf:
        pdf_url = download_pdf(
            meta["pdf_source_url"], px_id, new_version,
            local_dir=local_pdf_dir, gcs_bucket=gcs_bucket,
        ) or ""

    conn.execute(
        "INSERT INTO papers "
        "(px_id, version, title, author, date, abstract, "
        " primary_category, secondary_categories, repo, "
        " pages_url, github_url, pdf_url, is_current, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            px_id,
            new_version,
            meta["title"],
            meta["author"],
            meta["date"],
            meta["abstract"],
            meta["primary_category"],
            json.dumps(meta.get("secondary_categories", [])),
            repo,
            meta["pages_url"],
            meta["github_url"],
            pdf_url,
            content_hash,
        ),
    )
    conn.commit()

    log.info("%s %s v%d (%s)", action.upper(), px_id, new_version, meta["title"][:60])
    return px_id, new_version, action


# ---------------------------------------------------------------------------
# Bulk scrape (safety-net / initial population)
# ---------------------------------------------------------------------------

def list_repos(org: str) -> list[str]:
    """List repos in the org via GitHub API."""
    import urllib.request

    repos: list[str] = []
    page = 1
    while True:
        url = f"https://api.github.com/orgs/{org}/repos?per_page=100&page={page}"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
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


def scrape_all_repos(
    conn: sqlite3.Connection,
    org: str = "ParallelScience",
    skip_pdf: bool = False,
    local_pdf_dir: str | None = LOCAL_PDF_DIR,
    gcs_bucket: str | None = GCS_BUCKET,
) -> dict[str, int]:
    """Scrape all repos in the org. Returns counts: {new, updated, unchanged, failed}."""
    repos = list_repos(org)
    counts = {"new": 0, "updated": 0, "unchanged": 0, "failed": 0}

    print(f"Found {len(repos)} repos in {org}")
    for repo in repos:
        print(f"  {repo}...", end=" ", flush=True)
        meta = scrape_single_repo(org, repo)
        if not meta:
            print("skip")
            counts["failed"] += 1
            continue
        try:
            px_id, version, action = upsert_paper(
                conn, meta, org,
                local_pdf_dir=local_pdf_dir,
                gcs_bucket=gcs_bucket,
                skip_pdf=skip_pdf,
            )
            print(f"{action} → {px_id} v{version}")
            counts[action] += 1
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            counts["failed"] += 1

    return counts
