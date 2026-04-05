#!/usr/bin/env bash
set -euo pipefail

# Scrape papers into SQLite and redeploy to Cloud Run if changes found.
#
# The webhook handles real-time updates; this is a daily safety net.
#
# Crontab entry:
#   0 6 * * * /scratch/scratch-aiscientist/parallelscience/arxiv-browse/scripts/scrape_and_deploy.sh >> /tmp/parallel-arxiv-scrape.log 2>&1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
export PATH="$HOME/google-cloud-sdk/bin:$PATH"

cd "$REPO_DIR"

DB_PATH="browse/data/papers.db"

# Save checksum of current database
OLD_HASH=$(md5sum "$DB_PATH" 2>/dev/null | cut -d' ' -f1 || echo "none")

echo "$(date) — Scraping papers..."
python scripts/scrape_papers.py --db "$DB_PATH"

NEW_HASH=$(md5sum "$DB_PATH" | cut -d' ' -f1)

if [ "$OLD_HASH" = "$NEW_HASH" ]; then
  echo "$(date) — No changes. Skipping deploy."
  exit 0
fi

echo "$(date) — Changes detected. Deploying to Cloud Run..."
gcloud run deploy arxiv-browse \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8080 \
  --quiet

echo "$(date) — Deploy complete."
