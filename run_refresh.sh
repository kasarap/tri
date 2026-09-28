#!/bin/bash
# run_refresh.sh
#
# Cron wrapper for refresh_dashboard.py, following the same pattern as
# the Spotify playlist tool (throwaway python:3.11-slim container, no
# host Python needed).
#
# Layout assumed (adjust the two paths below):
#   APPDATA_DIR   - persistent appdata folder, holds refresh_dashboard.py,
#                   dashboard_template.html, requirements.txt, secrets.env
#   REPO_DIR      - your local clone of the GitHub repo backing the
#                   Cloudflare Pages site (must already be `git clone`d
#                   with a working push remote, e.g. via SSH deploy key)
#
# secrets.env (create this yourself, chmod 600, NEVER commit it) should
# contain lines like:
#   STRAVA_CLIENT_ID=xxxx
#   STRAVA_CLIENT_SECRET=xxxx
#   STRAVA_REFRESH_TOKEN=xxxx
#   ARC_RELAY_URL=http://127.0.0.1:8103
#   ARC_RELAY_API_KEY=xxxx      # generated from Arc Relay's own API Keys page
#
# Garmin data is pulled through your existing Arc-Relay-hosted
# garmin-connect-mcp server (reusing its already-authenticated session)
# rather than logging into Garmin independently from this script. --network
# host below is what lets this throwaway container reach Arc Relay's
# 127.0.0.1:8103 the same way the host itself does. If that Garmin session
# ever logs out, just run your usual garmin-connect-mcp interactive auth
# flow (MFA, base64 TOKENS_B64, Rebuild & Restart) — nothing here changes.

set -euo pipefail

APPDATA_DIR="/mnt/user/appdata/mallorca-dashboard"
REPO_DIR="/mnt/user/appdata/mallorca-dashboard/site-repo"   # <-- point this at your actual repo clone
OUTPUT_FILENAME="index.html"                                 # <-- change if your Pages site serves a different path

LOGFILE="$APPDATA_DIR/run.log"

echo "=== $(date -u +%FT%TZ) starting refresh ===" >> "$LOGFILE"

docker run --rm \
  --network host \
  -v "$APPDATA_DIR/refresh_dashboard.py:/app/refresh_dashboard.py:ro" \
  -v "$APPDATA_DIR/dashboard_template.html:/app/dashboard_template.html:ro" \
  -v "$APPDATA_DIR/requirements.txt:/app/requirements.txt:ro" \
  -v "$REPO_DIR:/repo" \
  --env-file "$APPDATA_DIR/secrets.env" \
  -e OUTPUT_PATH="/repo/$OUTPUT_FILENAME" \
  -e TEMPLATE_PATH=/app/dashboard_template.html \
  -w /app \
  python:3.11-slim \
  sh -c "pip install -q -r requirements.txt && python refresh_dashboard.py" \
  >> "$LOGFILE" 2>&1

cd "$REPO_DIR"
if ! git diff --quiet -- "$OUTPUT_FILENAME"; then
  git add "$OUTPUT_FILENAME"
  git commit -m "Auto-refresh training dashboard $(date -u +%F)" >> "$LOGFILE" 2>&1
  git push >> "$LOGFILE" 2>&1
  echo "$(date -u +%FT%TZ) pushed updated dashboard" >> "$LOGFILE"
else
  echo "$(date -u +%FT%TZ) no data changes, skipped commit" >> "$LOGFILE"
fi

echo "=== $(date -u +%FT%TZ) done ===" >> "$LOGFILE"
