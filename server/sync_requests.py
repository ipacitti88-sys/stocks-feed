#!/usr/bin/env python3
"""
Keep the server in step with the trading terminal's watchlist ("my list").

1. Pull the terminal's list from the stocks-feed repo (mylist branch, written by the
   terminal sync task) into the my_list table, and drop the webpage changes the
   terminal has already applied (see mylist.py).
2. Start tracking any stock on that list the server doesn't track yet, with its full
   price history. requests.txt (the older one-way request file) is still read too.
3. Re-export the feed if anything changed.

Removing a stock from your list never stops the scanner tracking it; it only leaves
your list.

    python3 sync_requests.py
"""
import re
import sys

import psycopg2
import yfinance as yf

import export_feed
import load_history
import mylist

TICKER_RE = re.compile(r"^[A-Z0-9-]{1,10}(\.(V|TO|CN|NE))?$")


def main() -> int:
    feed = export_feed.FEED_DIR
    if not (feed / ".git").exists():
        return 0
    export_feed.git("pull", "--rebase", "--quiet")
    wanted = []
    req = feed / "requests.txt"
    if req.exists():
        for line in req.read_text().splitlines():
            t = line.strip().upper()
            if t and not t.startswith("#") and TICKER_RE.match(t):
                wanted.append(t)

    conn = psycopg2.connect()
    added = 0
    changed = False
    try:
        res = mylist.pull(conn)
        if res is not None:
            wanted += res["tickers"]
            changed = bool(res["added"] or res["removed"])
            if changed:
                print(f"My list now {res['count']} stocks (+{', '.join(res['added']) or '0'}"
                      f" / -{', '.join(res['removed']) or '0'}).")
        with conn, conn.cursor() as cur:
            cur.execute(load_history.CREATE_SQL)
            cur.execute("SELECT ticker FROM watchlist WHERE active")
            known = {r[0] for r in cur.fetchall()}
        for t in dict.fromkeys(wanted):
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
                known.add(t)
            else:
                # no history yet (bad ticker or Yahoo hiccup): forget it so the next sync retries
                with conn, conn.cursor() as cur:
                    cur.execute("DELETE FROM watchlist WHERE ticker = %s AND source = 'terminal'", (t,))
    finally:
        conn.close()

    if added:
        print(f"Started tracking {added} stock(s) from the terminal watchlist.")
    if added or changed:
        export_feed.main()
        try:
            import dashboard
            dashboard.main()
        except Exception as e:
            print(f"dashboard rebuild failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
