#!/usr/bin/env python3
"""
Track stocks added to the trading terminal's watchlist.

Claude's watchlist sync writes requests.txt (one Yahoo ticker per line) to the
stocks-feed repo. This pulls the repo, adds any ticker the server doesn't track
yet, loads its full price history, and re-exports the feed so the page gets it.

    python3 sync_requests.py
"""
import re
import sys

import psycopg2
import yfinance as yf

import export_feed
import load_history

TICKER_RE = re.compile(r"^[A-Z0-9-]{1,10}(\.(V|TO|CN|NE))?$")


def main() -> int:
    feed = export_feed.FEED_DIR
    if not (feed / ".git").exists():
        return 0
    export_feed.git("pull", "--rebase", "--quiet")
    req = feed / "requests.txt"
    if not req.exists():
        return 0
    wanted = []
    for line in req.read_text().splitlines():
        t = line.strip().upper()
        if t and not t.startswith("#") and TICKER_RE.match(t):
            wanted.append(t)

    conn = psycopg2.connect()
    added = 0
    try:
        with conn, conn.cursor() as cur:
            cur.execute(load_history.CREATE_SQL)
            cur.execute("SELECT ticker FROM watchlist WHERE active")
            known = {r[0] for r in cur.fetchall()}
        for t in wanted:
            if t in known:
                continue
            try:
                name = (yf.Ticker(t).info or {}).get("shortName") or t
            except Exception:
                name = t
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO watchlist (ticker, name, source) VALUES (%s, %s, 'terminal') "
                    "ON CONFLICT (ticker) DO UPDATE SET active = TRUE",
                    (t, name),
                )
            if load_history.load(conn, t, name, "max"):
                added += 1
            else:
                # no history yet (bad ticker or Yahoo hiccup): forget it so the next sync retries
                with conn, conn.cursor() as cur:
                    cur.execute("DELETE FROM watchlist WHERE ticker = %s AND source = 'terminal'", (t,))
    finally:
        conn.close()

    if added:
        print(f"Added {added} stock(s) from the terminal watchlist.")
        export_feed.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
