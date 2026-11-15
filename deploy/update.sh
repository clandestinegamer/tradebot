#!/usr/bin/env bash
# Automatic updates. A systemd timer runs this every 5 minutes (see install-updater.sh).
#   1. Bot: if the `release` branch on GitHub has moved (it only moves after the tests pass),
#      pull it and rebuild. .env, data/ and the database are never touched.
#   2. Android app: copy the newest TradeBot.apk + version.json into data/app/, where the
#      phone app checks for updates.
set -euo pipefail
cd "$(dirname "$0")/.."

exec 9>/run/tradebot-update.lock
flock -n 9 || exit 0                                   # an update is already running

TOKEN=$(cat /root/.tradebot-github-token)
BASIC=$(printf 'x-access-token:%s' "$TOKEN" | base64 -w0)
REPO=$(git config --get remote.origin.url | sed -E 's#^https://github.com/##; s#\.git$##')

git -c http.extraHeader="Authorization: Basic $BASIC" fetch -q origin release
if [ "$(git rev-parse HEAD)" != "$(git rev-parse FETCH_HEAD)" ]; then
  echo "updating bot $(git rev-parse --short HEAD) -> $(git rev-parse --short FETCH_HEAD)"
  git reset -q --hard FETCH_HEAD
  docker compose up -d --build
  docker image prune -f >/dev/null || true
fi

mkdir -p data/app
python3 -I deploy/fetch_app.py "$REPO" data/app < /root/.tradebot-github-token
