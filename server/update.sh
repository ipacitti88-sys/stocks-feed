#!/usr/bin/env bash
# stocks-update: get the latest server scripts from the stocks-feed repo and install them.
set -euo pipefail
FEED="$HOME/stocks-feed"
APP="$HOME/stocks"
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/stocks_feed -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

echo "== Getting the latest scripts"
git -C "$FEED" pull --rebase --autostash --quiet

SRC="$FEED/server"
for f in "$SRC"/*.py "$SRC"/*.html "$SRC"/run_daily.sh "$SRC"/run_intraday.sh; do
  cp "$f" "$APP/"
done
chmod +x "$APP"/run_*.sh

echo "== Checking the schedule"
DAILY="15 15 * * 1-5 $APP/run_daily.sh"
INTRA="*/30 * * * 1-5 $APP/run_intraday.sh"
( { crontab -l 2>/dev/null || true; } | { grep -v -E "stocks/run_daily.sh|stocks/run_intraday.sh" || true; } ; echo "$DAILY"; echo "$INTRA" ) | crontab -

echo "== Updating the Yahoo data library"
"$APP/venv/bin/pip" install --quiet --upgrade yfinance

echo
echo "Done. Installed version: $(cat "$SRC/VERSION")"
