#!/usr/bin/env bash
# Run by cron each weekday: find new TSXV names, update prices, then screen.
cd "$(dirname "$0")" || exit 1
. ./stocks.env
{
  echo "=== $(date) ==="
  ./venv/bin/python discover.py || echo "discover failed; continuing with existing watchlist"
  [ -d "$HOME/stocks-feed/.git" ] && ./venv/bin/python sync_requests.py
  ./venv/bin/python load_history.py --period 5d && ./venv/bin/python screen.py
  ./venv/bin/python intraday_archive.py || echo "5-minute archive failed; continuing"
  ./venv/bin/python export_bars.py || echo "1-day chart export failed; continuing"
  flock -n /tmp/stocks-research.lock ./venv/bin/python universe.py --daily || echo "universe update skipped"
  ./venv/bin/python dashboard.py
  [ -d "$HOME/stocks-feed/.git" ] && ./venv/bin/python export_feed.py
} >> logs/daily.log 2>&1
