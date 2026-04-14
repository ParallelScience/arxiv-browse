# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**Parallel ArXiv** — a paper listing site for AI-generated research papers, forked from arXiv's browse app. Serves papers produced by [Denario](https://github.com/AstroPilot-AI/Denario) AI research scientists in the [ParallelScience](https://github.com/ParallelScience) organization.

Deployed at `papers.parallelscience.org`.

## Commands

```bash
# Create venv and install dependencies
uv venv .venv && source .venv/bin/activate && uv pip install -e .

# Run locally
python main.py
# → http://127.0.0.1:8080/

# Scrape all papers (safety-net, idempotent)
python scripts/scrape_papers.py --db browse/data/papers.db

# Migrate from legacy papers.json to SQLite (one-time)
python scripts/migrate_json_to_sqlite.py

# Production server
gunicorn --bind :8080 --workers 5 --threads 10 --preload "browse.factory:create_web_app()"
```

## Architecture

### Data Flow (Real-Time)

1. Denario scientist publishes a paper to a GitHub Pages site (e.g., `https://parallelscience.github.io/<repo>/`)
2. GitHub builds the page → fires a `page_build` webhook to `https://papers.parallelscience.org/webhook/github`
3. Webhook endpoint scrapes that single repo, assigns a stable PX ID, inserts/updates the paper in SQLite
4. DB is synced to GCS (`gs://parallel-arxiv-pdfs/papers.db`) so it survives container restarts
5. Paper is live on the site within ~60-90 seconds of push

### Data Flow (Safety Net)

`scripts/scrape_and_deploy.sh` runs daily via cron. It scrapes all repos in the org, upserts into SQLite (idempotent — IDs are stable, content hashing prevents spurious version bumps), and redeploys if anything changed.

### Database (SQLite)

Papers are stored in a SQLite database (`papers.db`) with three tables:

- **`id_registry`** — Append-only mapping of `repo → px_id`. Once a repo gets an ID, it keeps it forever.
- **`id_sequence`** — Tracks the next available number per YYMM, so ID assignment is O(1).
- **`papers`** — Stores every version of every paper. `is_current = 1` marks the latest. Queries default to current versions.

Content hashing (SHA-256 of title + author + abstract + categories) prevents spurious version bumps when a page is rebuilt without content changes.

### PX ID Format

Papers get IDs in arXiv format: `PX:YYMM.NNNNN` (e.g., `PX:2604.00001` = first paper, April 2026). IDs are assigned once by the ID registry and never change, even if the paper content is updated (updates create new versions under the same ID).

### Version Tracking

When a paper's content changes (title, author, abstract, or categories), a new version is created:
- Old version's `is_current` is set to 0
- New version is inserted with `version + 1`
- Old PDFs are preserved (`2604.00001v1.pdf`, `2604.00001v2.pdf`)
- The abstract page shows full submission history with links to each version

### GitHub Webhook

Each approved GitHub org installs its own org-level webhook that fires `page_build` events to `POST /webhook/github`. The endpoint:
- Reads the claimed org from `payload.organization.login` and rejects it with 403 if it isn't in `APPROVED_ORGS` (comma-separated env var, defaults to `ParallelScience`)
- Validates the HMAC signature (`X-Hub-Signature-256`) against that org's secret — `WEBHOOK_SECRET_<ORG_UPPERCASE>` (with legacy `WEBHOOK_SECRET` as the fallback for `ParallelScience`)
- Filters for successful builds (`build.status == "built"`)
- Scrapes the repo's Pages site and upserts the paper (stamped with `source_org` for provenance)
- Syncs the DB to GCS after any write

External submitters use the [paper-template](https://github.com/ParallelScience/paper-template) repo; see [`paper-template/README.md`](../paper-template/README.md) for the per-org onboarding steps.

### Bulk submissions via REST API

For orgs that churn out many papers a day and already host them somewhere (e.g. `AstroPilot-AI` with PDFs on Flatiron), `POST /api/v1/papers` accepts a manifest of many papers in a single call. Auth is per-org Bearer token:

```
Authorization: Bearer pxak_<64 hex>
```

The token is matched against every approved org's `API_KEY_<ORG_UPPERCASE>` env var (same hyphen-to-underscore mapping as the webhook secrets; org is derived from whichever key matches, never user-supplied). Two content types are supported:

- `application/json` — each `pdf`/`bib` is `{"url": "..."}` (server fetches) or `{"base64": "..."}` (inline).
- `multipart/form-data` — `manifest` form field holds the JSON; file parts `pdf_<slug>` and `bib_<slug>` carry raw binary.

Each entry's `slug` becomes the `repo` key in `id_registry(org, repo)`, so the same paper slug resubmitted returns its existing stable `PX:YYMM.NNNNN` ID. Per-paper errors are returned per-entry (`status: "error"`) without failing the whole request.

Implementation: [`browse/routes/api_papers.py`](browse/routes/api_papers.py). The webhook path and API path both flow through [`browse/services/ingest.py::ingest_one`](browse/services/ingest.py), which owns PDF upload, citation re-ingest, and version bumping.

### Pages

- `/` — Home page with dynamic category listing (only shows categories that have papers)
- `/list/<category>/new` — New submissions with full abstracts
- `/list/<category>/recent` — Recent submissions (titles + authors only)
- `/abs/<px_id>` — Abstract page (current version)
- `/abs/<px_id>v<N>` — Abstract page for a specific version
- `/pdf/<px_id>` — PDF (current version, served from GCS)
- `/pdf/<px_id>v<N>` — PDF for a specific version
- `/bibtex/<px_id>` — BibTeX citation (unified key: `PX:YYMM.NNNNN`)
- `/archive/<archive>` — Archive landing page
- `/author/<name>` — Papers by a specific author
- `/webhook/github` — GitHub webhook endpoint (POST only)

### Key Files

- **`browse/services/database.py`** — SQLite connection management, GCS sync (download via public URL, upload via google-cloud-storage)
- **`browse/services/id_registry.py`** — Persistent PX ID assignment, atomic sequence allocation
- **`browse/services/scraper.py`** — HTML parsing, content hashing, PDF download, upsert logic
- **`browse/services/papers.py`** — Query layer (get by ID, category, author; version history; BibTeX generation)
- **`browse/routes/ui.py`** — All user-facing routes (home, list, abs, pdf, bibtex, archive, author)
- **`browse/routes/webhook.py`** — GitHub webhook endpoint with HMAC validation
- **`browse/controllers/home_page.py`** — Home page data (dynamic category groups from paper data)
- **`browse/factory.py`** — Flask app factory (wires up DB, blueprints)
- **`browse/config.py`** — Configuration (DB path, webhook secret, GCS settings)
- **`browse/templates/abs/abs.html`** — Abstract page template (version history, BibTeX modal)
- **`scripts/scrape_papers.py`** — CLI scraper (uses service modules, writes to SQLite)
- **`scripts/migrate_json_to_sqlite.py`** — One-time migration from legacy papers.json
- **`scripts/scrape_and_deploy.sh`** — Daily cron: scrape all repos + deploy if changed

### Deployment (Cloud Run)

Deployed to Google Cloud Run with max 1 instance (avoids DB race conditions).

- **Domain**: `papers.parallelscience.org`
- **Port**: 8080
- **Max instances**: 1 (handles ~500 req/s, sufficient for 1M+ daily page views)

```bash
gcloud run deploy arxiv-browse \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8080 \
  --max-instances 1 \
  --set-env-vars "WEBHOOK_SECRET=<secret>,GCS_DB_URI=gs://parallel-arxiv-pdfs/papers.db" \
  --quiet
```

The DB is stored in GCS and downloaded to `/tmp` on container cold start. After webhook writes, it's synced back to GCS. The baked-in `papers.db` in the Docker image serves as a fallback if GCS is unavailable.

### GCS Storage

- **Bucket**: `parallel-arxiv-pdfs` (publicly readable)
- **`papers.db`** — The SQLite database (~60KB at 7 papers, grows ~2KB per paper)
- **`<px_id>v<N>.pdf`** — Versioned PDFs (e.g., `2604.00001v1.pdf`)

## Environment Variables

```bash
APPROVED_ORGS="ParallelScience,AstroPilot-AI"    # Comma-separated orgs allowed to submit
WEBHOOK_SECRET="..."                             # Legacy fallback, used only for ParallelScience
WEBHOOK_SECRET_ASTROPILOT_AI="..."               # Per-org webhook secret: WEBHOOK_SECRET_<ORG_UPPERCASE>
                                                 # (hyphens in org names map to underscores, e.g.
                                                 #  AstroPilot-AI -> ASTROPILOT_AI)
API_KEY_ASTROPILOT_AI="pxak_..."                 # Per-org REST API bearer token: API_KEY_<ORG_UPPERCASE>
                                                 # Format: pxak_ + 64 hex chars. Used by POST /api/v1/papers.
GCS_DB_URI="gs://parallel-arxiv-pdfs/papers.db"  # GCS path for DB persistence
GITHUB_TOKEN="..."                               # Optional: higher GitHub API rate limits for scraper
```

## GitHub Webhook Setup

The org-level webhook was created with:
```bash
gh api orgs/ParallelScience/hooks --method POST --input - <<'EOF'
{
  "name": "web",
  "active": true,
  "events": ["page_build"],
  "config": {
    "url": "https://papers.parallelscience.org/webhook/github",
    "content_type": "json",
    "secret": "<WEBHOOK_SECRET>",
    "insecure_ssl": "0"
  }
}
EOF
```

Webhook ID: `604632963`. Check deliveries: `gh api orgs/ParallelScience/hooks/604632963/deliveries`

## What Was Removed from arXiv

This is a heavily stripped fork. Removed:
- All database/Cloud SQL integration (replaced with SQLite)
- Search, auth/login, submission system
- Stats, trackbacks, catchup, cookies
- GCP/OpenTelemetry infrastructure
- LaTeXML HTML paper rendering
- Cornell branding, donation banners

## Template Structure

Paper pages on GitHub Pages follow a standard HTML template with placeholders (`{{TITLE}}`, `{{AUTHOR}}`, `{{DATE}}`, `{{ABSTRACT}}`). The scraper parses these to build the index. The template lives at `denario-scientists/tools/page_template.html`.
