#!/usr/bin/env bash
# stocks-update: get the latest server scripts from the stocks-feed repo and install them.
set -euo pipefail
FEED="$HOME/stocks-feed"
APP="$HOME/stocks"
SRC="$FEED/server"
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/stocks_feed -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

if [ -z "${STOCKS_UPDATE_FRESH:-}" ]; then
  echo "== Getting the latest scripts"
  git -C "$FEED" pull --rebase --autostash --quiet
  # run the freshly downloaded copy of this script, so new steps apply right away
  STOCKS_UPDATE_FRESH=1 exec bash "$SRC/update.sh"
fi

for f in "$SRC"/*.py "$SRC"/*.html "$SRC"/run_*.sh; do
  cp "$f" "$APP/"
done
chmod +x "$APP"/run_*.sh

echo "== Checking the schedule"
DAILY="15 15 * * 1-5 $APP/run_daily.sh"
INTRA="*/5 * * * 1-5 $APP/run_intraday.sh"
WEEKLY="0 8 * * 6 $APP/run_weekly.sh"
NEWS="30 6 * * 1-5 $APP/run_news.sh"
BACKUP="30 21 * * * $APP/run_backup.sh"
BACKUP_AM="35 7 * * * $APP/run_backup.sh"
LISTAPI="*/5 * * * * $APP/run_listapi.sh"
LISTAPI_BOOT="@reboot $APP/run_listapi.sh"
( { crontab -l 2>/dev/null || true; } | { grep -v -E "stocks/run_(daily|intraday|weekly|news|backup|listapi).sh" || true; } ; echo "$DAILY"; echo "$INTRA"; echo "$WEEKLY"; echo "$NEWS"; echo "$BACKUP"; echo "$BACKUP_AM"; echo "$LISTAPI"; echo "$LISTAPI_BOOT" ) | crontab -

echo "== Updating the Yahoo data library"
"$APP/venv/bin/pip" install --quiet --upgrade yfinance

VERSION="$(cat "$SRC/VERSION")"
echo
echo "Installed version: $VERSION"

# one-time steps that come with a new version (runs once per version)
DONE="$APP/.after_update_done"
if [ -x "$SRC/after_update.sh" ] && [ "$(cat "$DONE" 2>/dev/null || true)" != "$VERSION" ]; then
  echo
  bash "$SRC/after_update.sh"
  echo "$VERSION" > "$DONE"
fi
echo
echo "Done."
