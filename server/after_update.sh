#!/usr/bin/env bash
# One-time steps for this version, run automatically by stocks-update.
set -euo pipefail
cd "$HOME/stocks"
. ./stocks.env
mkdir -p logs
echo "== Applying the new liquidity rule: C\$100k a day (Venture), C\$50k a day (TSX)"
./venv/bin/python screen.py >/dev/null && echo "screen updated"
./venv/bin/python dashboard.py
./venv/bin/python export_feed.py >/dev/null && echo "feed updated"
grep -A6 "^## TSX Venture: flagged" "$HOME/stocks-feed/latest.md" | head -7 || true
echo
echo "== Re-running the signal lab with the new rule, in the background"
nohup setsid bash -c '. ./stocks.env; flock /tmp/stocks-research.lock bash -c "./venv/bin/python signal_lab.py --market tsxv; ./venv/bin/python signal_lab.py --market tsx"' >> logs/research.log 2>&1 < /dev/null &
sleep 1
echo "Done. You can close this window."
