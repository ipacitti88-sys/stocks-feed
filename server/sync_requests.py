#!/usr/bin/env python3
"""
Keep the server in step with the trading terminal's watchlist ("my list").

1. Pull the terminal's list from the stocks-feed repo (mylist branch, written by the
   terminal sync task) into the my_list table, and drop the webpage changes the
   terminal has already applied (see mylist.py).
2. Start tracking any stock on that list the server doesn't track yet, with its full
   price history: also stocks added on the webpage that the terminal hasn't applied
   yet, and every weekly pick (so the picks scorecard has prices). requests.txt (the
   older one-way request file) is still read too.
3. Re-export the feed if anything changed.

Removing a stock from your list never stops the scanner tracking it; it only leaves
your list.

    python3 sync_requests.py
"""
import datetime as dt
import json
import re
import sys

import psycopg2
import yfinance as yf

import export_feed
import load_history
import mylist

TICKER_RE = re.compile(r"^[A-Z0-9-]{1,10}(\.(V|TO|CN|NE))?$")
PICK_DAYS = 180  # weekly picks from this far back are tracked for the scorecard


def pick_tickers(conn) -> list[str]:
    """Tickers of recent weekly picks, from the terminal's backup (see terminal_backup.py)."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('terminal_backup') IS NOT NULL")
        if not cur.fetchone()[0]:
            return []
        cur.execute("SELECT data FROM terminal_backup WHERE collection = 'weeks' "
                    "AND COALESCE(data->>'date', doc_id) >= to_char(CURRENT_DATE - %s, 'YYYY-MM-DD')", (PICK_DAYS,))
        out = []
        for (d,) in cur.fetchall():
            d = d if isinstance(d, dict) else json.loads(d)
            for p in d.get("picks") or []:
                t = str((p or {}).get("ticker") or "").strip().upper()
                if mylist.TICKER_RE.match(t):
                    out.append(t)
        return out


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
        source = {t: "terminal" for t in wanted}
        with conn, conn.cursor() as cur:
            cur.execute(load_history.CREATE_SQL)
            cur.execute("SELECT ticker FROM watchlist WHERE active")
            known = {r[0] for r in cur.fetchall()}
            mylist.ensure(conn)
            cur.execute("SELECT DISTINCT ticker FROM list_changes WHERE action = 'add'")
            for (t,) in cur.fetchall():
                source.setdefault(t, "webpage")
                wanted.append(t)
        for t in pick_tickers(conn):
            source.setdefault(t, "picks")
            wanted.append(t)
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM my_list_state WHERE key = 'track_failed'")
            r = cur.fetchone()
        try:
            failed = json.loads(r[0]) if r and r[0] else {}
        except ValueError:
            failed = {}
        recent = (dt.date.today() - dt.timedelta(days=3)).isoformat()
        for t in dict.fromkeys(wanted):
            if t in known:
                continue
            if failed.get(t, "") >= recent:
                continue  # Yahoo had no prices for it lately: try again in a few days
            try:
                name = (yf.Ticker(t).info or {}).get("shortName") or t
            except Exception:
                name = t
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO watchlist (ticker, name, source) VALUES (%s, %s, %s) "
                    "ON CONFLICT (ticker) DO UPDATE SET active = TRUE",
                    (t, name, source.get(t, "terminal")),
                )
            if load_history.load(conn, t, name, "max"):
                added += 1
                known.add(t)
            else:
                # no history yet (bad ticker or Yahoo hiccup): forget it so the next sync retries
                failed[t] = dt.date.today().isoformat()
                with conn, conn.cursor() as cur:
                    cur.execute("DELETE FROM watchlist WHERE ticker = %s AND source = ANY(%s)",
                                (t, ["terminal", "webpage", "picks"]))
                    cur.execute("INSERT INTO my_list_state (key, value) VALUES ('track_failed', %s) "
                                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (json.dumps(failed),))
    finally:
        conn.close()

    if added:
        print(f"Started tracking {added} stock(s) from my list and the weekly picks.")
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
