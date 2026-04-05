#!/usr/bin/env bash
set -euo pipefail

# Scrape papers and redeploy to Cloud Run only if new papers found.
# Runs as a cron job on orion every 30 minutes.
#
# Crontab entry:
#   */30 * * * * /scratch/scratch-aiscientist/parallelscience/arxiv-browse/scripts/scrape_and_deploy.sh >> /tmp/parallel-arxiv-scrape.log 2>&1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
export PATH="$HOME/google-cloud-sdk/bin:$PATH"

cd "$REPO_DIR"

# Save checksum of current papers.json
OLD_HASH=$(md5sum browse/data/papers.json 2>/dev/null | cut -d' ' -f1 || echo "none")

echo "$(date) — Scraping papers..."
python scripts/scrape_papers.py

NEW_HASH=$(md5sum browse/data/papers.json | cut -d' ' -f1)

if [ "$OLD_HASH" = "$NEW_HASH" ]; then
  echo "$(date) — No new papers. Skipping deploy."
  exit 0
fi

echo "$(date) — New papers found. Deploying to Cloud Run..."
gcloud run deploy arxiv-browse \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8080 \
  --quiet

echo "$(date) — Deploy complete."
