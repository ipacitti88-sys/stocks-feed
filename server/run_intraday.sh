#!/usr/bin/env bash
# Run by cron every 5 min on weekdays; intraday.py exits at once when the market is closed.
# The lock skips a pass if the previous one is still running.
cd "$(dirname "$0")" || exit 1
. ./stocks.env
# watchlist requests from the repo: every 30 minutes is plenty
if [ -d "$HOME/stocks-feed/.git" ] && [ $(( 10#$(date +%M) % 30 )) -lt 5 ]; then
  ./venv/bin/python sync_requests.py >> logs/intraday.log 2>&1
fi
flock -n /tmp/stocks-intraday.lock ./venv/bin/python intraday.py >> logs/intraday.log 2>&1
# headlines for today's active stocks, every 30 minutes
if [ $(( 10#$(date +%M) % 30 )) -lt 5 ]; then
  flock -n /tmp/stocks-news.lock ./venv/bin/python news.py --hot >> logs/news.log 2>&1
fi
