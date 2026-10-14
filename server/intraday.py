#!/usr/bin/env python3
"""
Intraday scan: live (~15 min delayed) prices for the whole watchlist while the
market is open, flags stocks that are RISING, and refreshes the dashboard.

Cron runs it every 30 minutes on weekdays; outside market hours it exits
immediately, so the schedule doesn't need to know your time zone.

    python3 intraday.py          # normal run (skips if market is closed)
    python3 intraday.py --force  # run anyway (testing)

"Rising" means all of:
  - up RISE_PCT% or more from yesterday's close
  - still climbing: price higher than ~30 minutes ago
  - real buying behind it: volume pace >= RISE_PACE x normal for this time of day
  - liquid: 20-day average volume >= MIN_LIQUIDITY shares
"""
import argparse
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
import yfinance as yf

ET = ZoneInfo("America/Toronto")
OPEN, CLOSE = dt.time(9, 30), dt.time(16, 0)
SESSION_MIN = 390          # 9:30-16:00 ET
SKIP_FIRST_MIN = 15        # ignore the noisy first 15 minutes
RISE_PCT = 3.0
RISE_PACE = 1.5
MIN_LIQUIDITY = 50_000
CHUNK = 100                # tickers per Yahoo request

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS intraday_quotes (
    ticker          TEXT PRIMARY KEY,
    as_of           TIMESTAMPTZ NOT NULL,
    trade_date      DATE NOT NULL,
    last            NUMERIC(14,4),
    prev_close      NUMERIC(14,4),
    day_open        NUMERIC(14,4),
    day_high        NUMERIC(14,4),
    day_volume      BIGINT,
    pct_day         NUMERIC(8,2),
    pct_30m         NUMERIC(8,2),
    pace_rel_vol    NUMERIC(8,2),
    rising          BOOLEAN DEFAULT FALSE,
    first_rising_at TIMESTAMPTZ
);
"""

BARS_SQL = """
CREATE TABLE IF NOT EXISTS intraday_bars (
    ticker TEXT NOT NULL,
    ts     TIMESTAMPTZ NOT NULL,
    close  NUMERIC(14,4),
    volume BIGINT,
    PRIMARY KEY (ticker, ts)
);
CREATE TABLE IF NOT EXISTS benchmark_quotes (
    ticker     TEXT PRIMARY KEY,
    as_of      TIMESTAMPTZ,
    last       NUMERIC(14,4),
    prev_close NUMERIC(14,4)
);
CREATE TABLE IF NOT EXISTS rising_log (
    trade_date DATE NOT NULL,
    ticker     TEXT NOT NULL,
    flagged_at TIMESTAMPTZ NOT NULL,
    price      NUMERIC(14,4),
    pct_day    NUMERIC(8,2),
    pace       NUMERIC(8,2),
    PRIMARY KEY (trade_date, ticker)
);
"""

CONTEXT_SQL = """
SELECT w.ticker,
  (SELECT close FROM daily_prices d WHERE d.ticker = w.ticker AND d.trade_date < %(today)s
     ORDER BY trade_date DESC LIMIT 1) AS prev_close,
  (SELECT AVG(volume) FROM (SELECT volume FROM daily_prices d WHERE d.ticker = w.ticker
     AND d.trade_date < %(today)s ORDER BY trade_date DESC LIMIT 20) v) AS avg_vol20
FROM watchlist w WHERE w.active
"""

UPSERT_SQL = """
INSERT INTO intraday_quotes (ticker, as_of, trade_date, last, prev_close, day_open, day_high,
                             day_volume, pct_day, pct_30m, pace_rel_vol, rising, first_rising_at)
VALUES %s
ON CONFLICT (ticker) DO UPDATE SET
  as_of = EXCLUDED.as_of, trade_date = EXCLUDED.trade_date, last = EXCLUDED.last,
  prev_close = EXCLUDED.prev_close, day_open = EXCLUDED.day_open, day_high = EXCLUDED.day_high,
  day_volume = EXCLUDED.day_volume, pct_day = EXCLUDED.pct_day, pct_30m = EXCLUDED.pct_30m,
  pace_rel_vol = EXCLUDED.pace_rel_vol, rising = EXCLUDED.rising,
  -- keep the first time it started rising today; reset on a new day
  first_rising_at = CASE
    WHEN NOT EXCLUDED.rising THEN NULL
    WHEN intraday_quotes.trade_date = EXCLUDED.trade_date AND intraday_quotes.first_rising_at IS NOT NULL
      THEN intraday_quotes.first_rising_at
    ELSE EXCLUDED.as_of END;
"""


def market_minutes(now_et: dt.datetime) -> float:
    """Minutes since the open (can be negative or > SESSION_MIN)."""
    start = now_et.replace(hour=OPEN.hour, minute=OPEN.minute, second=0, microsecond=0)
    return (now_et - start).total_seconds() / 60


def fetch_bars(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Today's 5-minute bars per ticker, in Toronto time."""
    out = {}
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        df = yf.download(chunk, period="1d", interval="5m", group_by="ticker",
                         auto_adjust=False, threads=True, progress=False)
        if df is None or df.empty:
            continue
        for t in chunk:
            try:
                sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                continue
            idx = sub.index
            sub.index = idx.tz_convert(ET) if idx.tz is not None else idx.tz_localize("UTC").tz_convert(ET)
            out[t] = sub
    return out


def evaluate(bars: pd.DataFrame, prev_close, avg_vol20, now_et: dt.datetime):
    today = now_et.date()
    bars = bars[bars.index.date == today]
    if bars.empty:
        return None
    last = float(bars["Close"].iloc[-1])
    as_of = bars.index[-1].to_pydatetime() + dt.timedelta(minutes=5)  # bar end time
    day_vol = int(bars["Volume"].fillna(0).sum())
    day_open = float(bars["Open"].iloc[0])
    day_high = float(bars["High"].max())

    # price ~30 minutes ago (6 five-minute bars back), else the open
    past = bars[bars.index <= bars.index[-1] - pd.Timedelta(minutes=30)]
    ref30 = float(past["Close"].iloc[-1]) if not past.empty else day_open
    pct_30m = (last / ref30 - 1) * 100 if ref30 else None

    pct_day = (last / float(prev_close) - 1) * 100 if prev_close else None

    elapsed = min(max(market_minutes(as_of.astimezone(ET)), 1), SESSION_MIN)
    expected = float(avg_vol20) * max(elapsed / SESSION_MIN, 0.08) if avg_vol20 else None
    pace = day_vol / expected if expected else None

    rising = bool(
        pct_day is not None and pct_day >= RISE_PCT
        and pct_30m is not None and pct_30m > 0
        and pace is not None and pace >= RISE_PACE
        and avg_vol20 is not None and float(avg_vol20) >= MIN_LIQUIDITY
        and market_minutes(now_et) >= SKIP_FIRST_MIN
    )
    r = lambda v, n=2: round(v, n) if v is not None else None
    return dict(as_of=as_of, trade_date=today, last=r(last, 4), prev_close=prev_close,
                day_open=r(day_open, 4), day_high=r(day_high, 4), day_volume=day_vol,
                pct_day=r(pct_day), pct_30m=r(pct_30m), pace=r(pace), rising=rising)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="run even if the market is closed")
    args = ap.parse_args()

    now_et = dt.datetime.now(ET)
    mins = market_minutes(now_et)
    if not args.force and (now_et.weekday() >= 5 or mins < SKIP_FIRST_MIN or mins > SESSION_MIN + 10):
        return 0  # market closed: nothing to do

    conn = psycopg2.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            cur.execute(BARS_SQL)
            cur.execute(CONTEXT_SQL, {"today": now_et.date()})
            context = {t: (pc, av) for t, pc, av in cur.fetchall()}

        bars = fetch_bars(sorted(context))
        if not bars:
            print(f"{now_et:%H:%M} ET: no intraday data (holiday or Yahoo issue)")
            return 0

        rows, rising, log_rows = [], [], []
        for t, b in bars.items():
            prev_close, avg_vol20 = context.get(t, (None, None))
            ev = evaluate(b, prev_close, avg_vol20, now_et)
            if not ev:
                continue
            rows.append((t, ev["as_of"], ev["trade_date"], ev["last"], ev["prev_close"],
                         ev["day_open"], ev["day_high"], ev["day_volume"], ev["pct_day"],
                         ev["pct_30m"], ev["pace"], ev["rising"], ev["as_of"] if ev["rising"] else None))
            if ev["rising"]:
                rising.append((t, ev["pct_day"], ev["pace"]))
                log_rows.append((ev["trade_date"], t, ev["as_of"], ev["last"], ev["pct_day"], ev["pace"]))

        # market index, live (same 5-minute data as the stocks)
        bench_rows = []
        try:
            import benchmarks
            bb = fetch_bars(list(benchmarks.BENCHMARKS))
            with conn.cursor() as cur:
                for sym, b in bb.items():
                    b = b[b.index.date == now_et.date()]
                    if b.empty:
                        continue
                    cur.execute("SELECT close FROM daily_prices WHERE ticker = %s AND trade_date < %s "
                                "ORDER BY trade_date DESC LIMIT 1", (sym, now_et.date()))
                    r = cur.fetchone()
                    bench_rows.append((sym, b.index[-1].to_pydatetime() + dt.timedelta(minutes=5),
                                       round(float(b["Close"].iloc[-1]), 4), r[0] if r else None))
        except Exception as e:
            print(f"benchmark quote failed: {e}", file=sys.stderr)

        today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        bar_rows = []
        for t, b in bars.items():
            tb = b[b.index >= today_start]
            for ts, r in tb.iterrows():
                if pd.notna(r["Close"]):
                    vol = int(r["Volume"]) if pd.notna(r["Volume"]) else None
                    bar_rows.append((t, ts.to_pydatetime(), round(float(r["Close"]), 4), vol))

        with conn, conn.cursor() as cur:
            execute_values(cur, UPSERT_SQL, rows)
            if bench_rows:
                execute_values(cur, "INSERT INTO benchmark_quotes (ticker, as_of, last, prev_close) VALUES %s "
                                    "ON CONFLICT (ticker) DO UPDATE SET as_of = EXCLUDED.as_of, last = EXCLUDED.last, "
                                    "prev_close = EXCLUDED.prev_close", bench_rows)
            # permanent record of the first time each stock went Rising each day
            if log_rows:
                execute_values(cur, "INSERT INTO rising_log (trade_date, ticker, flagged_at, price, pct_day, pace) "
                                    "VALUES %s ON CONFLICT (trade_date, ticker) DO NOTHING", log_rows)
            # keep only today's 5-minute bars (for the 1D chart)
            cur.execute("DELETE FROM intraday_bars WHERE ts < %s", (today_start,))
            if bar_rows:
                execute_values(cur, "INSERT INTO intraday_bars (ticker, ts, close, volume) VALUES %s "
                                    "ON CONFLICT (ticker, ts) DO UPDATE SET close = EXCLUDED.close, volume = EXCLUDED.volume",
                               bar_rows, page_size=1000)
    finally:
        conn.close()

    rising.sort(key=lambda x: -x[1])
    print(f"{now_et:%Y-%m-%d %H:%M} ET: {len(rows)} quotes, {len(rising)} rising"
          + "".join(f"\n  {t:10} +{p}%  pace {v}x" for t, p, v in rising))

    import dashboard
    rc = dashboard.main()
    if (Path.home() / "stocks-feed" / ".git").exists():
        import export_feed
        export_feed.main()
    return rc


if __name__ == "__main__":
    sys.exit(main())
