#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
mkdir -p logs
echo "== Rising phone alerts are now off (they lost money in 3 months of live flags). RISING_ALERTS=1 in stocks.env turns them back on."
echo "== Washout watch for the last close"
./venv/bin/python washout.py || echo "  washout check failed; it runs again with the daily job"
./venv/bin/python dashboard.py >/dev/null || true
echo "Done. New: a Washout watch card on the Scanner tab, washout lists in the feed, and a phone alert when a TSX washout fires at the close."
