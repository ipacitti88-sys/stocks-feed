#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
# Covers the TSX addition and the 1-day charts (safe to run more than once).
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
mkdir -p logs

echo "== Finding TSX stocks (same rules as the Venture: C\$0.10-C\$5, 50,000+ shares a day)"
echo "   Loads a year of history for each new one, about 10 minutes. Leave this window open."
./venv/bin/python discover.py 2>&1 | grep -E "Yahoo returned|new to your watchlist|failed" || true

echo
echo "== Building 1-day chart files for every archived day (webpage)"
./venv/bin/python intraday_archive.py --export-all

echo
echo "== Publishing the last 20 days of 5-minute bars for the terminal"
./venv/bin/python export_bars.py || echo "(will retry after the next close)"

echo
echo "== Rebuilding the webpage (Venture and TSX tabs) and the terminal feed"
./venv/bin/python dashboard.py
./venv/bin/python export_feed.py >/dev/null && echo "feed updated"

echo
echo "== Starting the TSX research download in the background (all TSX listings, 10 years)"
nohup setsid ./run_research.sh >/dev/null 2>&1 < /dev/null &
sleep 2
echo "Running on its own now. You can close this window."
echo "To peek at progress:  tail -n 5 ~/stocks/logs/research.log"
