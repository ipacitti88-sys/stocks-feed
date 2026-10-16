#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
mkdir -p logs
./venv/bin/python dashboard.py >/dev/null
echo "== Fetching the latest news for every tracked stock, in the background (about 10 minutes)"
nohup setsid ./run_news.sh >/dev/null 2>&1 < /dev/null &
sleep 1
echo "Running on its own. You can close this window."
echo "To check:  tail -n 3 ~/stocks/logs/news.log"
