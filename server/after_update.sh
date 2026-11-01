#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
mkdir -p logs "$HOME/recap"
# The recap now runs inside run_daily.sh, so drop any hand-made weekly_recap cron line.
( crontab -l 2>/dev/null || true ) | { grep -v "weekly_recap" || true; } | crontab -
echo "== Building the weekly recap"
./venv/bin/python weekly_recap.py "$HOME/stocks-feed" "${WEB_DIR:-$PWD/web}/recap.html" "$HOME/recap/ledger.json" \
  || echo "  recap failed; it runs again with the daily job"
./venv/bin/python dashboard.py >/dev/null || true
echo "Done. New: a Weekly recap tab on the webpage, rebuilt after every weekday close."
