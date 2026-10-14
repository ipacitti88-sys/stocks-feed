#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env

echo "== Re-testing the buy signals, split by stock price"
./venv/bin/python signal_lab.py
