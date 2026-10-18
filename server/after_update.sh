#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
mkdir -p logs

echo "== Restarting the list service (it now also saves price alerts set on the webpage)"
pkill -f "python listapi.py" 2>/dev/null || true
sleep 1
nohup ./run_listapi.sh >/dev/null 2>&1 &
sleep 3
if curl -fsS "http://localhost:${LIST_API_PORT:-8081}/mylist" >/dev/null 2>&1; then
  echo "List service is running."
else
  echo "List service didn't answer yet; cron retries every 5 minutes. Log: ~/stocks/logs/listapi.log"
fi
./venv/bin/python mylist.py --status || true

echo "== Phone alerts during the trading day (ntfy)"
if ! grep -qE "^(export )?NTFY_TOPIC=" stocks.env; then
  TOPIC="stocks-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  {
    echo ""
    echo "# phone alerts through ntfy (see notify.py); empty = off"
    echo "export NTFY_TOPIC=$TOPIC"
  } >> stocks.env
  export NTFY_TOPIC="$TOPIC"
  echo "Created a private notification topic."
fi
. ./stocks.env
echo "  1. Install the free ntfy app on your phone (App Store or Google Play)."
echo "  2. Tap + and subscribe to this topic (keep it private):"
echo "       $NTFY_TOPIC"
./venv/bin/python notify.py --test || echo "  Couldn't reach ntfy.sh just now; try later: cd ~/stocks && . ./stocks.env && ./venv/bin/python notify.py --test"

echo "== Weekly picks scorecard: start tracking past picks (prices load in the background)"
nohup ./venv/bin/python sync_requests.py >> logs/intraday.log 2>&1 &

echo "== Dilution check: scanning headlines for share sales (about 15 minutes, in the background)"
nohup bash -c "flock /tmp/stocks-news.lock ./venv/bin/python news.py --all && ./venv/bin/python dashboard.py" >> logs/news.log 2>&1 &

./venv/bin/python dashboard.py >/dev/null || true
echo "Done. New on the webpage: a weekly picks scorecard (Track record tab), price alerts you can set on any stock's"
echo "detail panel, phone notifications during the day, and FINANCING tags on stocks that recently sold shares."
