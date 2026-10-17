#!/usr/bin/env bash
# Keeps the webpage's "my list" service running (port 8081). Cron starts it at boot and
# checks every 5 minutes; the lock makes this exit at once when it's already running.
cd "$(dirname "$0")" || exit 1
. ./stocks.env
mkdir -p logs
exec flock -n /tmp/stocks-listapi.lock ./venv/bin/python listapi.py >> logs/listapi.log 2>&1
