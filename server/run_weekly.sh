#!/usr/bin/env bash
# Run by cron Saturday morning: pick up new listings, then re-run the research.
cd "$(dirname "$0")" || exit 1
. ./stocks.env
{
  echo "=== $(date) ==="
  flock -n /tmp/stocks-research.lock ./venv/bin/python universe.py --backfill
  ./venv/bin/python patterns.py
  ./venv/bin/python signal_lab.py
} >> logs/weekly.log 2>&1
