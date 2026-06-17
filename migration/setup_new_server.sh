#!/usr/bin/env bash
# Bootstrap a NEW Vultr server for the weather bot — run from the repo root after `git clone`.
#
# Safe to run: it ONLY builds the venv + installs deps + builds the frontend.
# It does NOT create .env, copy the DB, or start the service — those are sequenced
# manually in migration/README.md (secrets + the split-brain cutover).
#
# Usage:  cd /root/weather_trading_bot && bash migration/setup_new_server.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
echo ">> Repo: $REPO_DIR"

# 0. Geoblock sanity — refuse to set up on a still-blocked IP.
echo ">> Checking Polymarket geoblock for this server's IP..."
GEO="$(curl -s --max-time 15 https://polymarket.com/api/geoblock || echo '{}')"
echo "   $GEO"
if echo "$GEO" | grep -q '"blocked":true'; then
  echo "!! This IP is GEO-BLOCKED by Polymarket. Pick a different region. Aborting." >&2
  exit 1
fi

# 1. Python venv + deps (path must match weatherbot.service: ./venv)
echo ">> Creating venv + installing Python deps (this is the slow part)..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 2. Frontend build (frontend/dist is gitignored — must be rebuilt on every box)
echo ">> Installing + building frontend..."
( cd frontend && npm install && npm run build )

echo
echo ">> Done. Remaining MANUAL steps (see migration/README.md):"
echo "   1. cp migration/.env.template .env  &&  edit real values (KELLY_FRACTION=0.05 etc.)"
echo "   2. Stop old box, scp tradingbot.db over (cutover — avoid running two bots)."
echo "   3. cp migration/weatherbot.service /etc/systemd/system/ && systemctl enable --now weatherbot"
