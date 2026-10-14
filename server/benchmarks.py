#!/usr/bin/env python3
"""
Market benchmarks: the indexes your stocks are compared against.

Their daily closes live in daily_prices like any stock, but they are NOT in the
watchlist, so the screens never treat them as stocks to buy.

    python3 benchmarks.py          # update (first run loads 2 years)
"""
import datetime as dt
import sys

import psycopg2

BENCHMARKS = {
    "^SPCDNX": "S&P/TSX Venture Composite",
    "^GSPTSE": "S&P/TSX Composite",
}
MAIN = "^SPCDNX"


def update(conn, period="5d"):
    import load_history
    for sym, name in BENCHMARKS.items():
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM daily_prices WHERE ticker = %s", (sym,))
            have = cur.fetchone()[0]
        load_history.load(conn, sym, name, period if have > 200 else "2y")


def history(conn, days=420):
    """{symbol: [(date, close), ...]} for the last `days` calendar days."""
    since = dt.date.today() - dt.timedelta(days=days)
    with conn.cursor() as cur:
        cur.execute("SELECT ticker, trade_date, close FROM daily_prices "
                    "WHERE ticker = ANY(%s) AND trade_date >= %s AND close IS NOT NULL "
                    "ORDER BY ticker, trade_date", (list(BENCHMARKS), since))
        rows = cur.fetchall()
    out = {}
    for t, d, c in rows:
        out.setdefault(t, []).append((d, float(c)))
    return out


def change(series, days_back=None, since=None):
    """% change of the last close vs the close `days_back` trading days ago, or vs the last close before `since`."""
    if not series:
        return None
    last = series[-1][1]
    if since is not None:
        before = [c for d, c in series if d < since]
        base = before[-1] if before else None
    else:
        base = series[-1 - days_back][1] if len(series) > days_back else None
    return round((last / base - 1) * 100, 2) if base else None


def summary(conn):
    """What the feed publishes for each benchmark."""
    out = {}
    hist = history(conn)
    live = {}
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('benchmark_quotes') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("SELECT ticker, as_of, last, prev_close FROM benchmark_quotes")
            for t, as_of, last, prev in cur.fetchall():
                live[t] = (as_of, float(last) if last is not None else None, float(prev) if prev else None)
    for sym, name in BENCHMARKS.items():
        s = hist.get(sym, [])
        if not s:
            continue
        last_d, last_c = s[-1]
        e = {"name": name, "date": str(last_d), "last": round(last_c, 2),
             "prev": round(s[-2][1], 2) if len(s) > 1 else None,
             "chg_1d": change(s, 1), "chg_5d": change(s, 5), "chg_1m": change(s, 21),
             "chg_3m": change(s, 63), "chg_1y": change(s, 252),
             "chg_ytd": change(s, since=dt.date(last_d.year, 1, 1)),
             "dates": [str(d) for d, _ in s], "closes": [round(c, 2) for _, c in s]}
        lv = live.get(sym)
        if lv and lv[0] and lv[1] and lv[2] and lv[0].date() > last_d:
            e["live"] = {"as_of": lv[0].isoformat(), "last": round(lv[1], 2),
                         "pct_day": round((lv[1] / lv[2] - 1) * 100, 2)}
        out[sym] = e
    return out


if __name__ == "__main__":
    c = psycopg2.connect()
    try:
        update(c)
        f = lambda v: "-" if v is None else f"{v:+.1f}%"
        for sym, e in summary(c).items():
            print(f"{e['name']:28} {e['last']:>10,.2f} on {e['date']}  "
                  f"day {f(e['chg_1d'])}  month {f(e['chg_1m'])}  year {f(e['chg_1y'])}")
    finally:
        c.close()
    sys.exit(0)
