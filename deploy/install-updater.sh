#!/usr/bin/env bash
# One-time setup for automatic updates. Run as root from a fresh git clone of the repo:
#   - moves .env and data/ over from the existing /root/tradebot install (if there is one),
#   - puts this checkout at /root/tradebot and starts the bot from it,
#   - installs a timer that runs deploy/update.sh every 5 minutes.
# Needs the GitHub token saved in /root/.tradebot-github-token (read-only access to the repo).
set -euo pipefail
NEW=$(cd "$(dirname "$0")/.." && pwd)
LIVE=/root/tradebot
[ -s /root/.tradebot-github-token ] || { echo "Save your GitHub token in /root/.tradebot-github-token first."; exit 1; }
chmod 600 /root/.tradebot-github-token

if [ "$NEW" != "$LIVE" ]; then
  if [ -d "$LIVE" ]; then
    echo "Moving your settings and data from the old install…"
    [ -f "$LIVE/.env" ] && cp -n "$LIVE/.env" "$NEW/.env"
    (cd "$LIVE" && docker compose down) || true
    if [ -d "$LIVE/data" ]; then rm -rf "$NEW/data"; mv "$LIVE/data" "$NEW/data"; fi
    OLD="/root/tradebot-old-$(date +%Y%m%d-%H%M)"
    mv "$LIVE" "$OLD"
    echo "Old install kept at $OLD (safe to delete once everything works)."
  fi
  mv "$NEW" "$LIVE"
fi

cd "$LIVE"
[ -f .env ] || cp .env.example .env
mkdir -p data/app && chown -R 1000:1000 data
chmod +x deploy/update.sh

cat > /etc/systemd/system/tradebot-update.service <<UNIT
[Unit]
Description=TradeBot automatic update
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=$LIVE/deploy/update.sh
UNIT

cat > /etc/systemd/system/tradebot-update.timer <<UNIT
[Unit]
Description=Check for TradeBot updates every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
echo "Starting the bot…"
docker compose up -d --build
systemctl enable --now tradebot-update.timer
systemctl start tradebot-update.service || true
echo
echo "Automatic updates are on. See what they did with:  journalctl -u tradebot-update -n 30"
