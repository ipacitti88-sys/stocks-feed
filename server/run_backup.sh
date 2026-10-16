#!/usr/bin/env bash
# Run by cron at 10:30pm every day: load the terminal's nightly backup into the database.
cd "$(dirname "$0")" || exit 1
. ./stocks.env
mkdir -p logs
{
  echo "=== $(date) ==="
  ./venv/bin/python terminal_backup.py
} >> logs/backup.log 2>&1
