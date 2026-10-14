#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
mkdir -p logs
echo "== Re-testing the signals at five liquidity levels (both markets), in the background"
# waits for any research run still in progress, then runs both markets
nohup setsid bash -c '. ./stocks.env; flock /tmp/stocks-research.lock bash -c "./venv/bin/python signal_lab.py --market tsxv; ./venv/bin/python signal_lab.py --market tsx"' >> logs/research.log 2>&1 < /dev/null &
sleep 1
echo "Running on its own (about 10-20 minutes). You can close this window."
echo "To check:  grep -A7 'Liquidity test' ~/stocks/logs/research.log | tail -16"
