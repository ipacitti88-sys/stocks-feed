#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env

echo "== Loading the market indexes (2 years)"
./venv/bin/python benchmarks.py

echo
echo "== Publishing to the terminal feed"
./venv/bin/python export_feed.py >/dev/null && echo "feed updated"

echo
echo "== Re-testing the buy signals against the market"
./venv/bin/python signal_lab.py | head -3
