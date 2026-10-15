#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
./venv/bin/python dashboard.py >/dev/null && echo "Webpage rebuilt (it now refreshes itself every 5 minutes during market hours)."
echo "Live scan schedule:"; crontab -l | grep run_intraday
