#!/usr/bin/env bash
# Run by cron at 6:30am on weekdays: headlines for every tracked stock (overnight and morning releases),
# then publish them with the chart data so the 6:40am sync brings them to the terminal.
cd "$(dirname "$0")" || exit 1
. ./stocks.env
{
  echo "=== $(date) ==="
  flock /tmp/stocks-news.lock ./venv/bin/python news.py --all
  ./venv/bin/python export_bars.py
} >> logs/news.log 2>&1
