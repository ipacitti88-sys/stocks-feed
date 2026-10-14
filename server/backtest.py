#!/usr/bin/env python3
"""
Backtest the "Flagged" signal over the price history already in the database.

Replays the same rule the daily screen uses, day by day for the past year:
  flagged = 2 or more of
            close above its 50-day average,
            volume more than 2x its 20-day average,
            close at a 52-week high (or the highest close available),
  and 20-day average volume of at least 50,000 shares.
A stock flagged again within 7 calendar days is not counted twice.
Each signal records its close and the next 20 closes.

Writes backtest.json to the stocks-feed repo and pushes it, so the trading
terminal's Track record tab can show a year of results.

    python3 backtest.py                    # signals before the live log began
    python3 backtest.py --until 2026-10-13
"""
import argparse
import datetime as dt
import json
import sys
from collections import defaultdict

import psycopg2

import export_feed

MIN_LIQUIDITY = 50_000
MIN_HISTORY = 60        # trading days needed before a day can be judged
DEDUPE_DAYS = 7
HORIZON = 20

SQL = """
WITH p AS (
  SELECT d.ticker, d.trade_date, d.close, d.volume,
    AVG(d.close)  OVER w50 AS ma50,
    AVG(d.volume) OVER (PARTITION BY d.ticker ORDER BY d.trade_date
                        ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS av20,
    MAX(d.close)  OVER (PARTITION BY d.ticker ORDER BY d.trade_date ROWS 251 PRECEDING) AS hi252,
    ROW_NUMBER()  OVER (PARTITION BY d.ticker ORDER BY d.trade_date) AS rn
  FROM daily_prices d
  JOIN watchlist w ON w.ticker = d.ticker AND w.active
  WHERE d.trade_date >= %(since)s
  WINDOW w50 AS (PARTITION BY d.ticker ORDER BY d.trade_date ROWS 49 PRECEDING)
)
SELECT p.ticker, COALESCE(w.name, p.ticker), p.trade_date, p.close, p.volume, p.ma50, p.av20, p.hi252, p.rn
FROM p JOIN watchlist w ON w.ticker = p.ticker
ORDER BY p.ticker, p.trade_date
"""


def num(v):
    return float(v) if v is not None else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="2026-10-13",
                    help="only log signals before this date (the live log starts here)")
    ap.add_argument("--days", type=int, default=365, help="how far back to look for signals")
    args = ap.parse_args()
    until = dt.date.fromisoformat(args.until)
    start = until - dt.timedelta(days=args.days)
    since = start - dt.timedelta(days=120)  # extra history so averages are ready

    conn = psycopg2.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(SQL, {"since": since})
            rows = cur.fetchall()
    finally:
        conn.close()

    by_ticker = defaultdict(list)
    for t, name, d, close, vol, ma50, av20, hi252, rn in rows:
        by_ticker[t].append((name, d, num(close), num(vol), num(ma50), num(av20), num(hi252), rn))

    dates = defaultdict(list)
    total = 0
    for t, series in by_ticker.items():
        last_sig = None
        closes = [r[2] for r in series]
        for i, (name, d, close, vol, ma50, av20, hi252, rn) in enumerate(series):
            if d < start or d >= until or rn < MIN_HISTORY:
                continue
            if not close or close <= 0 or av20 is None or av20 < MIN_LIQUIDITY or vol is None:
                continue
            signals = int(ma50 is not None and close > ma50) \
                + int(vol > 2 * av20) \
                + int(hi252 is not None and close >= hi252)
            if signals < 2:
                continue
            if last_sig and (d - last_sig).days <= DEDUPE_DAYS:
                continue
            last_sig = d
            path = [round(c, 4) for c in closes[i + 1:i + 1 + HORIZON] if c is not None]
            dates[d.isoformat()].append({
                "ticker": t, "name": name, "type": "flagged", "signals": signals,
                "entry": round(close, 4), "path": path,
            })
            total += 1

    out = {
        "generated": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
        "rule": "Flagged: 2+ of above 50-day average, volume over 2x its 20-day average, "
                "52-week high; 20-day average volume at least 50,000; no repeat within 7 days",
        "from": start.isoformat(), "until": until.isoformat(),
        "signals": total, "days": len(dates),
        "dates": dict(sorted(dates.items())),
    }

    # quick summary in the terminal
    def summary(k):
        r = [e["path"][k - 1] / e["entry"] - 1 for es in dates.values() for e in es if len(e["path"]) >= k]
        if not r:
            return f"{k:>2}d: no data"
        wins = sum(1 for x in r if x > 0)
        return f"{k:>2}d: {len(r)} signals, {wins / len(r):.0%} won, average {sum(r) / len(r):+.1%}"
    print(f"Backtest {start} to {until}: {total} flagged signals on {len(dates)} days")
    for k in (1, 5, 20):
        print("  " + summary(k))

    feed = export_feed.FEED_DIR
    if not (feed / ".git").exists():
        print("No stocks-feed repo; wrote nothing.", file=sys.stderr)
        return 1
    export_feed.git("pull", "--rebase", "--quiet")
    (feed / "backtest.json").write_text(json.dumps(out, separators=(",", ":")))
    export_feed.git("add", "backtest.json")
    if export_feed.git("diff", "--cached", "--quiet").returncode != 0:
        export_feed.git("commit", "-q", "-m", "backtest " + out["generated"])
        r = export_feed.git("push", "-q")
        if r.returncode != 0:
            print("Push failed:\n" + r.stderr, file=sys.stderr)
            return 1
    print("backtest.json pushed to stocks-feed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
