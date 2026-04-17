#!/usr/bin/env bash
set -euo pipefail

# Daily scrape of yesterday's arXiv papers for citations to Parallel ArXiv
# papers, merged into the authoritative papers.db on GCS and pushed live.
#
# Flow:
#   1. Download the current GCS DB (authoritative, webhook-maintained).
#   2. Run the scraper against that DB — writes only to external_citations
#      and external_scraper_state.
#   3. Merge the scraper's writes into a *fresh* GCS snapshot (captures any
#      webhook writes that landed during the scrape window), then upload.
#   4. Force a Cloud Run cold start so the running container reloads the
#      DB (otherwise it keeps using its cached /tmp copy).
#   5. Ping the stats API to refresh its cache (otherwise 30-min polling).
#
# Crontab entry:
#   30 4 * * * /scratch/scratch-aiscientist/parallelscience/arxiv-browse/scripts/scrape_external_citations_and_deploy.sh >> /tmp/parallel-arxiv-ext-scrape.log 2>&1
#
# 04:30 UTC is chosen to run well before scrape_and_deploy.sh (06:00) and
# away from peak webhook traffic, narrowing the race window between our
# download and our merged upload.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
export PATH="$HOME/google-cloud-sdk/bin:$PATH"

cd "$REPO_DIR"

GCS_URI="${GCS_DB_URI:-gs://parallel-arxiv-pdfs/papers.db}"
WORK_DB="/tmp/parallel-arxiv-ext-scrape.db"

echo "$(date) — Downloading authoritative DB from $GCS_URI..."
gsutil cp "$GCS_URI" "$WORK_DB"

OLD_HASH=$(md5sum "$WORK_DB" | cut -d' ' -f1)

echo "$(date) — Scraping external arXiv citations..."
python scripts/scrape_external_citations.py --db "$WORK_DB"

NEW_HASH=$(md5sum "$WORK_DB" | cut -d' ' -f1)

if [ "$OLD_HASH" = "$NEW_HASH" ]; then
  echo "$(date) — No new citations. Skipping merge + deploy."
  rm -f "$WORK_DB"
  exit 0
fi

echo "$(date) — Changes detected. Merging into fresh GCS snapshot..."
python scripts/merge_external_citations_to_gcs.py \
  --local "$WORK_DB" \
  --gcs-uri "$GCS_URI"

echo "$(date) — Forcing Cloud Run cold start so new DB is picked up..."
gcloud run services update arxiv-browse \
  --region us-central1 \
  --update-env-vars "FORCE_COLD_START=$(date +%s)" \
  --quiet

# Best-effort refresh of parallel-science-api. Skipped silently if the env
# vars aren't set — its 30-min poll will catch up anyway.
if [ -n "${STATS_API_REFRESH_URL:-}" ] && [ -n "${STATS_API_ADMIN_KEY:-}" ]; then
  echo "$(date) — Pinging parallel-science-api to refresh its DB..."
  curl -s -X POST -H "X-Admin-Key: $STATS_API_ADMIN_KEY" "$STATS_API_REFRESH_URL" \
    -o /dev/null -w "stats-api refresh: %{http_code}\n" || true
fi

rm -f "$WORK_DB"
echo "$(date) — Done."
