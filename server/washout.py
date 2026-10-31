#!/usr/bin/env python3
"""
Washout watch: the "Bounce after a sharp drop" signal, before and when it fires.

The signal lab's washout rule is: down 20%+ over 5 trading days, then an up day on
above-normal volume (signal_lab.signal_masks). On the TSX it is the strongest signal
after real bid/ask spreads, but only held about 20 trading days with no tight stop;
held 1 to 5 days it lost money. On the TSX Venture it loses money after spreads.

Because the rule needs a 20% drop first, it fires with no warning. This script lists:
  fired   liquid stocks where the washout fired at the last close
  watch   liquid stocks down 15%+ over the last 5 days that haven't fired yet
          ("ready" = already down 20%+, so one up day on normal volume would fire it)

Same liquidity and price rules as the scanner (C$50k a day, C$0.10-5.00).

    python3 washout.py            # print both lists
    python3 washout.py --alert    # also push a phone alert for each new washout (TSX by default)

Alerts go through notify.py, once per stock per day (table live_alerts, kind "washout").
WASHOUT_ALERT_MARKETS in stocks.env picks the markets: "tsx" (default), "tsx,tsxv", or empty for none.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import psycopg2

import benchmarks

WATCH_DROP = -0.15      # on the watch list from a 15% 5-day drop
TRIGGER_DROP = -0.20    # the signal's own threshold
FEED_DIR = Path(os.environ.get("FEED_DIR", Path.home() / "stocks-feed"))
CLICK = os.environ.get("DASHBOARD_URL", "")


def _r(v, n=1):
    return None if v is None or v != v else round(float(v), n)


def scan(conn) -> dict:
    """Both lists for the latest trading day: {"date", "fired": [...], "watch": [...]}."""
    import signal_lab as sl
    df = sl.add_indicators(sl.load_daily(conn, dt.date.today() - dt.timedelta(days=200)))
    if df.empty:
        return {"date": None, "fired": [], "watch": []}
    fired_mask = sl.signal_masks(df)["washout"]
    day = df["date"].max()
    elig = (df["tracked"] & (df["date"] == day) & df["close"].between(sl.MIN_PRICE, sl.MAX_PRICE)
            & (df["dv20"] >= df["ticker"].map(benchmarks.min_dollars)) & (df["rn"] >= sl.MIN_HISTORY))
    d5 = df["close"] / df["close5"] - 1
    vr = df["volume"] / df["av20"]
    out = {"date": str(day), "fired": [], "watch": []}
    for i in df.index[elig & ((d5 <= WATCH_DROP) | fired_mask)]:
        row = df.loc[i]
        t = row["ticker"]
        item = dict(ticker=t, name=row["name"], market=benchmarks.market_of(t), close=_r(row["close"], 4),
                    pct_5d=_r(100 * d5[i]), pct_day=_r(100 * (row["close"] / row["prev"] - 1)) if row["prev"] else None,
                    rel_vol=_r(vr[i]), dollars_day=int(row["dv20"]) if row["dv20"] == row["dv20"] else None)
        if fired_mask[i]:
            out["fired"].append(item)
        else:
            item["ready"] = bool(d5[i] <= TRIGGER_DROP)
            out["watch"].append(item)
    out["fired"].sort(key=lambda x: x["pct_5d"])
    out["watch"].sort(key=lambda x: x["pct_5d"])
    return out


def lab_result(market: str) -> dict | None:
    """The signal lab's washout result after real spreads for this market, if a lab file is around."""
    f = FEED_DIR / ("signal_lab_tsx.json" if market == "tsx" else "signal_lab.json")
    try:
        d = json.loads(f.read_text())
        c = next(c for c in d["costs"] if c["key"] == "washout")
        return {"avg": c["avg_est"], "exit": c["exit"], "n": c["n"]}
    except Exception:
        return None


def alert_markets() -> set[str]:
    raw = os.environ.get("WASHOUT_ALERT_MARKETS", "tsx")
    return {m.strip() for m in raw.split(",") if m.strip()}


def px(v: float) -> str:
    return f"C${v:.3f}" if v < 1 else f"C${v:.2f}"


def alert(conn, res: dict) -> list[dict]:
    """Record and push today's new washouts in the alert markets. Returns the new ones."""
    import live_alerts
    import notify
    markets = alert_markets()
    cand = [x for x in res["fired"] if x["market"] in markets]
    if not cand or not res["date"]:
        return []
    new = []
    with conn, conn.cursor() as cur:
        cur.execute(live_alerts.CREATE_SQL)
        for x in cand:
            cur.execute("INSERT INTO live_alerts (trade_date, ticker, kind, level, price, pct_day) "
                        "VALUES (%s, %s, 'washout', NULL, %s, %s) ON CONFLICT DO NOTHING",
                        (res["date"], x["ticker"], x["close"], x["pct_day"]))
            if cur.rowcount:
                new.append(x)
    if not new or not notify.enabled():
        return new
    lines = [f"{x['ticker']} at {px(x['close'])}: down {abs(x['pct_5d']):.0f}% in 5 days, "
             f"up {x['pct_day'] or 0:.1f}% today on {x['rel_vol'] or 0:.1f}x volume" for x in new]
    labs = {m: lab_result(m) for m in {x["market"] for x in new}}
    for m, L in labs.items():
        if L:
            where = "TSX" if m == "tsx" else "TSX Venture"
            lines.append(f"{where} washouts over 10 years: {L['avg']:+.1f}% a trade after real spreads, "
                         f"({L['exit']}, no tight stop). Shorter holds lost money.")
    lines.append("Not financial advice.")
    title = f"Washout: {new[0]['ticker']}" if len(new) == 1 else f"{len(new)} washouts at the close"
    if notify.send(title, "\n".join(lines), "chart_with_downwards_trend", priority=4, click=CLICK):
        with conn, conn.cursor() as cur:
            for x in new:
                cur.execute("UPDATE live_alerts SET sent = TRUE WHERE trade_date = %s AND ticker = %s AND kind = 'washout'",
                            (res["date"], x["ticker"]))
    return new


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alert", action="store_true", help="push a phone alert for each new washout")
    a = ap.parse_args()
    conn = psycopg2.connect()
    try:
        res = scan(conn)
        print(f"Washout watch for {res['date']}")
        for m, label in (("tsx", "TSX"), ("tsxv", "TSX Venture")):
            f = [x for x in res["fired"] if x["market"] == m]
            w = [x for x in res["watch"] if x["market"] == m]
            print(f"\n{label}: {len(f)} fired, {len(w)} on watch")
            for x in f:
                print(f"  FIRED {x['ticker']:10} {x['close']:>8}  5d {x['pct_5d']:+.1f}%  day {x['pct_day']:+.1f}%  vol {x['rel_vol']}x")
            for x in w:
                print(f"  {'ready' if x['ready'] else 'watch'} {x['ticker']:10} {x['close']:>8}  5d {x['pct_5d']:+.1f}%  "
                      f"C${(x['dollars_day'] or 0) / 1000:,.0f}k a day")
        if a.alert:
            new = alert(conn, res)
            print(f"\n{len(new)} new washout alert(s) (markets: {', '.join(sorted(alert_markets())) or 'none'}).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
