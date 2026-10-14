#!/usr/bin/env bash
# Builds the research universe (every TSX Venture listing, 10 years), then runs the
# pattern study and the signal lab on it. Safe to re-run: the download resumes.
cd "$(dirname "$0")" || exit 1
. ./stocks.env
mkdir -p logs
exec 9>/tmp/stocks-research.lock
flock -n 9 || { echo "Research run already in progress."; exit 0; }
{
  echo "=== $(date) research run started ==="
  ./venv/bin/python universe.py --backfill
  echo
  ./venv/bin/python patterns.py --market tsxv
  ./venv/bin/python patterns.py --market tsx
  echo
  ./venv/bin/python signal_lab.py --market tsxv
  ./venv/bin/python signal_lab.py --market tsx
  echo "=== $(date) research run finished ==="
} >> logs/research.log 2>&1
