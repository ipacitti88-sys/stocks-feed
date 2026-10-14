#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
echo "== Publishing breakouts and the market trend to the terminal feed"
./venv/bin/python export_feed.py >/dev/null && echo "feed updated"
grep -A4 "^## Breakouts" "$HOME/stocks-feed/latest.md" || true
