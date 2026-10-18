#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
mkdir -p logs
# from the previous version, in case it was skipped: phone alert topic
if ! grep -qE "^(export )?NTFY_TOPIC=" stocks.env; then
  TOPIC="stocks-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  printf '\n# phone alerts through ntfy (see notify.py); empty = off\nexport NTFY_TOPIC=%s\n' "$TOPIC" >> stocks.env
  echo "Phone alerts: install the ntfy app and subscribe to: $TOPIC"
fi
# also from the previous version (harmless to repeat)
echo "== Restarting the list service (price alerts from the webpage)"
pkill -f "python listapi.py" 2>/dev/null || true
sleep 1
nohup ./run_listapi.sh >/dev/null 2>&1 &
. ./stocks.env
echo "== Phone alerts: install the free ntfy app and subscribe to this topic (keep it private): $NTFY_TOPIC"
./venv/bin/python notify.py --test || echo "  Couldn't reach ntfy.sh just now; later run: cd ~/stocks && . ./stocks.env && ./venv/bin/python notify.py --test"
echo "== Tracking past weekly picks and scanning headlines for share sales, in the background"
nohup ./venv/bin/python sync_requests.py >> logs/intraday.log 2>&1 &
nohup bash -c "flock /tmp/stocks-news.lock ./venv/bin/python news.py --all && ./venv/bin/python dashboard.py" >> logs/news.log 2>&1 &

echo "== Trading costs for every tracked stock"
./venv/bin/python costs.py || true
./venv/bin/python dashboard.py >/dev/null || true
echo "== Re-running the signal lab with the new cost and stop-loss studies, in the background (about an hour; results reach the webpage and terminal when it finishes)"
nohup bash -c "flock /tmp/stocks-research.lock ./venv/bin/python signal_lab.py --market tsxv && flock /tmp/stocks-research.lock ./venv/bin/python signal_lab.py --market tsx && ./venv/bin/python dashboard.py" >> logs/weekly.log 2>&1 &
echo "== Scoreboard: which analysis works best so far"
./venv/bin/python scoreboard.py || true
./venv/bin/python dashboard.py >/dev/null || true
echo "Done. New: a scoreboard of every analysis (Track record tab). Each stock's page also shows its trading cost, a position size you can get out of, and a suggested stop-loss."
