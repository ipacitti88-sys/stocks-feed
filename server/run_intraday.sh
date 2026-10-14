#!/usr/bin/env bash
# Run by cron every 30 min on weekdays; intraday.py exits at once when the market is closed.
cd "$(dirname "$0")" || exit 1
. ./stocks.env
[ -d "$HOME/stocks-feed/.git" ] && ./venv/bin/python sync_requests.py >> logs/intraday.log 2>&1
./venv/bin/python intraday.py >> logs/intraday.log 2>&1
