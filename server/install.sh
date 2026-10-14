#!/usr/bin/env bash
# One-time: add the stocks-update command, install the latest scripts, run the backtest.
set -euo pipefail
mkdir -p "$HOME/.local/bin"
ln -sf "$HOME/stocks-feed/server/update.sh" "$HOME/.local/bin/stocks-update"
chmod +x "$HOME/stocks-feed/server/update.sh"
grep -q "alias stocks-update=" "$HOME/.bashrc" 2>/dev/null || echo "alias stocks-update='bash \$HOME/stocks-feed/server/update.sh'" >> "$HOME/.bashrc"

bash "$HOME/stocks-feed/server/update.sh"

echo
echo "== Backtesting the Flagged signal over the past year"
cd "$HOME/stocks" && . ./stocks.env && ./venv/bin/python backtest.py

echo
echo "All set. From now on, update the server with one command:  stocks-update"
echo "(Open a new terminal window first, or run: source ~/.bashrc)"
