#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
mkdir -p logs
echo "== Setting up the nightly terminal backup"
./venv/bin/python terminal_backup.py --setup
./venv/bin/python terminal_backup.py || true
echo "Done. The terminal's records are copied to your database every night at 10:30pm."
