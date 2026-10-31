#!/usr/bin/env python3
"""
Alerts during the trading day, pushed to your phone (see notify.py).

intraday.py calls check() after every 5-minute pass. It sends a notification when:
  - a stock with a buy level trades at or below it      ("buy")
  - a stock with a stop-loss trades at or below it      ("stop")
  - a stock on my list starts Rising (up 3%+, still climbing, heavy volume)  ("rising"),
    only if RISING_ALERTS=1 in stocks.env. Off by default since Oct 30, 2026: three months
    of live Rising flags lost money on average, behind buying any liquid stock at the same time.

The levels are the terminal's price alerts, with any you set on the webpage applied
(mylist.alert_levels). Prices are Yahoo's, about 15 minutes delayed. Each alert fires
at most once per stock and kind per day (table live_alerts). The terminal's own
end-of-day alerts, based on the close, carry on as before.

    python3 live_alerts.py            # check now against the latest intraday quotes
    python3 live_alerts.py --status   # today's alerts
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

import psycopg2

import mylist
import notify

ET = ZoneInfo("America/Toronto")
CLICK = os.environ.get("DASHBOARD_URL", "")
RISING = os.environ.get("RISING_ALERTS", "0").strip().lower() in ("1", "yes", "true", "on")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS live_alerts (
    trade_date DATE NOT NULL,
    ticker     TEXT NOT NULL,
    kind       TEXT NOT NULL,          -- buy, stop, rising or washout (washout.py)
    level      NUMERIC(14,4),
    price      NUMERIC(14,4),
    pct_day    NUMERIC(8,2),
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent       BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (trade_date, ticker, kind)
);
"""


def px(v) -> str:
    v = float(v)
    return f"C${v:.3f}" if v < 1 else f"C${v:.2f}"


def line(a: dict) -> str:
    t, p = a["ticker"], px(a["price"])
    day = f" ({a['pct_day']:+.1f}% today)" if a.get("pct_day") is not None else ""
    if a["kind"] == "buy":
        return f"{t} at {p}{day}: at or below your buy level {px(a['level'])}"
    if a["kind"] == "stop":
        return f"{t} at {p}{day}: below your stop-loss {px(a['level'])}"
    return f"{t} rising: {p}{day}, volume {a['pace']:.1f}x normal" if a.get("pace") else f"{t} rising: {p}{day}"


def check(conn, today: dt.date | None = None) -> list[dict]:
    """Record and push any new alerts from today's intraday quotes. Returns the new ones."""
    today = today or dt.datetime.now(ET).date()
    with conn, conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        cur.execute("SELECT to_regclass('intraday_quotes') IS NOT NULL")
        if not cur.fetchone()[0]:
            return []
        cur.execute("SELECT ticker, last, pct_day, pace_rel_vol, rising FROM intraday_quotes "
                    "WHERE trade_date = %s AND last IS NOT NULL", (today,))
        quotes = {t: (float(l), float(p) if p is not None else None, float(v) if v is not None else None, bool(r))
                  for t, l, p, v, r in cur.fetchall()}
    st = mylist.state(conn)
    levels = {t: a for t, a in st["alerts"].items() if a.get("buy") or a.get("stop")}
    mine = {i["t"] for i in st["items"] if i.get("pending") != "remove"}

    cand = []
    for t, a in levels.items():
        q = quotes.get(t)
        if not q:
            continue
        last, pday, pace, _ = q
        for kind in ("stop", "buy"):  # a stop is the more urgent of the two
            lvl = a.get(kind)
            if lvl and last <= float(lvl):
                cand.append({"ticker": t, "kind": kind, "level": float(lvl), "price": last, "pct_day": pday, "pace": pace})
    for t in (mine if RISING else ()):
        q = quotes.get(t)
        if q and q[3]:
            cand.append({"ticker": t, "kind": "rising", "level": None, "price": q[0], "pct_day": q[1], "pace": q[2]})

    new = []
    with conn, conn.cursor() as cur:
        for c in cand:
            cur.execute("INSERT INTO live_alerts (trade_date, ticker, kind, level, price, pct_day) "
                        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        (today, c["ticker"], c["kind"], c["level"], c["price"], c["pct_day"]))
            if cur.rowcount:
                new.append(c)
    if not new or not notify.enabled():
        return new

    order = {"stop": 0, "buy": 1, "rising": 2}
    new.sort(key=lambda c: (order[c["kind"]], c["ticker"]))
    kinds = {c["kind"] for c in new}
    if len(new) == 1:
        c = new[0]
        title = {"buy": f"Buy level hit: {c['ticker']}", "stop": f"Stop-loss hit: {c['ticker']}",
                 "rising": f"Rising: {c['ticker']}"}[c["kind"]]
    else:
        title = f"{len(new)} stock alerts"
    body = "\n".join(line(c) for c in new) + "\nPrices about 15 min delayed. Not financial advice."
    tags = "rotating_light" if "stop" in kinds else ("moneybag" if "buy" in kinds else "chart_with_upwards_trend")
    if notify.send(title, body, tags, priority=4 if kinds & {"stop", "buy"} else 3, click=CLICK):
        with conn, conn.cursor() as cur:
            for c in new:
                cur.execute("UPDATE live_alerts SET sent = TRUE WHERE trade_date = %s AND ticker = %s AND kind = %s",
                            (today, c["ticker"], c["kind"]))
    return new


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    conn = psycopg2.connect()
    try:
        if a.status:
            with conn, conn.cursor() as cur:
                cur.execute(CREATE_SQL)
                cur.execute("SELECT at, ticker, kind, level, price, sent FROM live_alerts "
                            "WHERE trade_date = (SELECT MAX(trade_date) FROM live_alerts) ORDER BY at")
                rows = cur.fetchall()
            print(f"{len(rows)} alert(s) on the latest day." + ("" if notify.enabled() else " Notifications are off (NTFY_TOPIC not set)."))
            for at, t, k, lvl, p, sent in rows:
                print(f"  {at.astimezone(ET):%H:%M} ET  {t:10} {k:6} price {p}  level {lvl or '-'}  {'sent' if sent else 'not sent'}")
            levels = mylist.alert_levels(conn)
            print(f"{len(levels)} stock(s) with buy or stop-loss levels. "
                  f"Rising alerts {'on' if RISING else 'off (RISING_ALERTS=1 turns them back on)'}.")
            return 0
        new = check(conn)
        print(f"{len(new)} new alert(s)." + "".join("\n  " + line(c) for c in new))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
