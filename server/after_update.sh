#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
mkdir -p logs
echo "== Setting up \"my list\" (the watchlist shared with the trading terminal)"
./venv/bin/python mylist.py || true
echo "== Starting the list service the webpage uses to add and remove stocks (port ${LIST_API_PORT:-8081})"
nohup ./run_listapi.sh >/dev/null 2>&1 &
sleep 3
if curl -fsS "http://localhost:${LIST_API_PORT:-8081}/mylist" >/dev/null 2>&1; then
  echo "List service is running."
else
  echo "List service didn't answer yet; cron retries every 5 minutes. Log: ~/stocks/logs/listapi.log"
fi
if command -v ufw >/dev/null 2>&1 && sudo -n ufw status 2>/dev/null | grep -q "Status: active"; then
  sudo -n ufw allow from 192.168.0.0/16 to any port "${LIST_API_PORT:-8081}" proto tcp >/dev/null || true
  sudo -n ufw allow in on tailscale0 to any port "${LIST_API_PORT:-8081}" proto tcp >/dev/null || true
  echo "Firewall: port ${LIST_API_PORT:-8081} opened for your home network and Tailscale."
fi
echo "== Loading the terminal's latest backup and rebuilding the webpage"
./venv/bin/python terminal_backup.py || true
./venv/bin/python dashboard.py || true
echo "Done. The webpage now has My list, Track record, Reports, Meme stocks and Paper account tabs."
