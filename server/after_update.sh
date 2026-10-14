#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
echo "== Building 1-day chart files for every archived day (webpage)"
./venv/bin/python intraday_archive.py --export-all
echo
echo "== Publishing the last 20 days of 5-minute bars for the terminal"
./venv/bin/python export_bars.py
echo
echo "== Rebuilding the webpage"
./venv/bin/python dashboard.py
