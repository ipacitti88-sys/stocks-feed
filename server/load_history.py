#!/usr/bin/env python3
"""
Load daily price history from Yahoo Finance into Postgres.

The tickers come from the `watchlist` table, so adding a stock is one command:
    python3 load_history.py --add XYZ.V "Company Name"   # adds it + loads full history

Daily update (what cron runs):
    python3 load_history.py --period 5d

Connection settings come from PGHOST / PGDATABASE / PGUSER and ~/.pgpass.
Safe to re-run: existing rows are updated, not duplicated.
Yahoo suffixes: .TO = TSX, .V = TSX Venture, none = US.
"""
import argparse
import sys
import time

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
import yfinance as yf

SEED = {
    "CEMX.TO": "CEMATRIX",
    "REAL.TO": "Real Matters",
    "ONE.V": "01 Quantum",
}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS daily_prices (
    ticker      TEXT        NOT NULL,
    trade_date  DATE        NOT NULL,
    open        NUMERIC(14,4),
    high        NUMERIC(14,4),
    low         NUMERIC(14,4),
    close       NUMERIC(14,4),
    adj_close   NUMERIC(14,4),
    volume      BIGINT,
    loaded_at   TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (ticker, trade_date)
);
CREATE TABLE IF NOT EXISTS watchlist (
    ticker   TEXT PRIMARY KEY,
    name     TEXT,
    active   BOOLEAN DEFAULT TRUE,
    added_on DATE DEFAULT CURRENT_DATE
);
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE;
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'manual';
"""

UPSERT_SQL = """
INSERT INTO daily_prices (ticker, trade_date, open, high, low, close, adj_close, volume)
VALUES %s
ON CONFLICT (ticker, trade_date) DO UPDATE SET
    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
    close = EXCLUDED.close, adj_close = EXCLUDED.adj_close,
    volume = EXCLUDED.volume, loaded_at = now();
"""


def fetch(ticker: str, period: str) -> list[tuple]:
    df = yf.Ticker(ticker).history(period=period, auto_adjust=False)
    if df.empty:
        return []
    # a consolidation/split re-prices all past data: reload everything so old and new prices match
    if period != "max" and "Stock Splits" in df and (df["Stock Splits"].fillna(0) != 0).any():
        print(f"{ticker:10} split/consolidation detected; reloading full history")
        return fetch(ticker, "max")
    rows = []
    for ts, r in df.iterrows():
        if pd.isna(r["Close"]):
            continue
        rows.append((
            ticker,
            ts.date(),
            float(r["Open"]), float(r["High"]), float(r["Low"]),
            float(r["Close"]), float(r.get("Adj Close", r["Close"])),
            int(r["Volume"]) if not pd.isna(r["Volume"]) else None,
        ))
    return rows


def load(conn, ticker: str, name: str, period: str) -> bool:
    try:
        rows = fetch(ticker, period)
    except Exception as e:  # network or Yahoo hiccup: skip, keep going
        print(f"{ticker:10} FAILED: {e}", file=sys.stderr)
        return False
    if not rows:
        print(f"{ticker:10} no data returned (check the ticker suffix: .TO / .V)")
        return False
    with conn, conn.cursor() as cur:
        execute_values(cur, UPSERT_SQL, rows)
    print(f"{ticker:10} {len(rows):5} rows  {rows[0][1]} to {rows[-1][1]}  ({name})")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="max",
                    help="Yahoo period: max, 5y, 1y, 3mo, 5d ... (default: max)")
    ap.add_argument("--add", nargs="+", metavar=("TICKER", "NAME"),
                    help="add a ticker to the watchlist and load its full history")
    args = ap.parse_args()

    conn = psycopg2.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            execute_values(
                cur,
                "INSERT INTO watchlist (ticker, name) VALUES %s ON CONFLICT (ticker) DO NOTHING",
                list(SEED.items()),
            )

        if args.add:
            ticker = args.add[0].upper()
            name = " ".join(args.add[1:]) or ticker
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO watchlist (ticker, name) VALUES (%s, %s) "
                    "ON CONFLICT (ticker) DO UPDATE SET name = EXCLUDED.name, active = TRUE",
                    (ticker, name),
                )
            ok = load(conn, ticker, name, "max")
            return 0 if ok else 1

        with conn.cursor() as cur:
            cur.execute("SELECT ticker, COALESCE(name, ticker) FROM watchlist WHERE active ORDER BY ticker")
            tickers = cur.fetchall()

        failures = 0
        for t, n in tickers:
            failures += not load(conn, t, n, args.period)
            time.sleep(0.3)  # gentle pacing so Yahoo doesn't rate-limit a long watchlist
        try:  # market indexes for comparison (not in the watchlist, so never screened)
            import benchmarks
            benchmarks.update(conn, args.period)
        except Exception as e:
            print(f"benchmark update failed: {e}", file=sys.stderr)
        return 1 if failures == len(tickers) else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
