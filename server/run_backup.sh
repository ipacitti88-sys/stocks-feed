#!/usr/bin/env bash
# Run by cron at 7:35am and 10:30pm every day: load the terminal's backup (pre-market notes,
# weekly picks, meme reviews, track record, paper account) into the database, then rebuild
# the webpage so its terminal tabs show it.
cd "$(dirname "$0")" || exit 1
. ./stocks.env
mkdir -p logs
{
  echo "=== $(date) ==="
  ./venv/bin/python terminal_backup.py
  ./venv/bin/python dashboard.py || echo "dashboard rebuild failed"
} >> logs/backup.log 2>&1
