#!/usr/bin/env bash
# Run by cron Saturday morning: re-test the buy signals with the week's new data.
cd "$(dirname "$0")" || exit 1
. ./stocks.env
{
  echo "=== $(date) ==="
  ./venv/bin/python signal_lab.py
} >> logs/weekly.log 2>&1
