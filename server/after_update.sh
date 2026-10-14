#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
mkdir -p logs
echo "== Starting the research download in the background"
nohup setsid ./run_research.sh >/dev/null 2>&1 < /dev/null &
sleep 2
echo "It's running on its own now (about 2 to 4 hours). You can close this window."
echo "To peek at progress:  tail -n 5 ~/stocks/logs/research.log"
