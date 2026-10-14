#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
mkdir -p logs

echo "== Finding TSX stocks (same rules as the Venture: C\$0.10-C\$5, 50,000+ shares a day)"
echo "   This loads a year of history for each new one, so it takes a while (about 10 minutes)."
./venv/bin/python discover.py 2>&1 | grep -E "Yahoo returned|new to your watchlist" || true

echo
echo "== Rebuilding the webpage (now with Venture and TSX tabs) and the terminal feed"
./venv/bin/python dashboard.py
./venv/bin/python export_feed.py >/dev/null && echo "feed updated"

echo
echo "== Starting the TSX research download in the background (all TSX listings, 10 years)"
nohup setsid ./run_research.sh >/dev/null 2>&1 < /dev/null &
sleep 2
echo "Running on its own now. You can close this window."
echo "To peek at progress:  tail -n 5 ~/stocks/logs/research.log"
