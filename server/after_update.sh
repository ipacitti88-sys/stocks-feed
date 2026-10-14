#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
mkdir -p logs

echo "== Saving the 5-minute history Yahoo still has (about 60 days, takes a few minutes)"
./venv/bin/python intraday_archive.py --backfill

echo
echo "== Testing six buy signals over the past year"
./venv/bin/python signal_lab.py
