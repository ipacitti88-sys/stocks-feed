#!/usr/bin/env python3
"""
Research universe: EVERY TSX Venture and TSX stock Yahoo lists, at any price or volume,
with up to 10 years of daily history. Used only for research (patterns,
signal tests) - never screened as buy candidates, so your scanner and terminal
don't change.

Why: testing only today's ~450 active stocks hides the ones that sank, which
makes cheap stocks look better than they were. Keeping every listing - and
keeping their data even after they drop off Yahoo - fixes most of that.

    python3 universe.py --backfill   # first run: find all listings, load 10 years (hours)
    python3 universe.py --daily      # weekday update: last 5 days for every listing
    python3 universe.py --refresh    # just update the list of listings
    (re-running --backfill only loads stocks it hasn't loaded yet, so it's also the weekly step)

Stock consolidations (reverse splits) are common on the Venture. When one
happens, that stock's whole history is re-downloaded so old prices stay
comparable with new ones.
"""
import argparse
import datetime as dt
import sys
import time

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
import yfinance as yf

import load_history

CHUNK = 50
PAUSE = 4  # seconds between Yahoo requests

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS universe (
    ticker      TEXT PRIMARY KEY,
    name        TEXT,
    exchange    TEXT,
    first_seen  DATE DEFAULT CURRENT_DATE,
    last_seen   DATE DEFAULT CURRENT_DATE,
    history_at  TIMESTAMPTZ,
    rows        INTEGER
);
"""


def list_exchange(code: str) -> list[dict]:
    from yfinance import EquityQuery
    from discover import is_common
    q = EquityQuery("and", [EquityQuery("eq", ["exchange", code]),
                            EquityQuery("gt", ["intradayprice", 0])])
    found, offset = [], 0
    while True:
        res = yf.screen(q, offset=offset, size=250, sortField="ticker", sortAsc=True)
        quotes = res.get("quotes", [])
        found += quotes
        offset += len(quotes)
        if not quotes or offset >= res.get("total", offset):
            break
        time.sleep(PAUSE)
    return [q for q in found if q.get("symbol") and is_common(q)]


EXCHANGES = {"VAN": "TSXV", "TOR": "TSX"}


def list_tsxv() -> list[dict]:  # kept for older callers
    return list_exchange("VAN")


def refresh(conn) -> int:
    rows = []
    for code, label in EXCHANGES.items():
        try:
            quotes = list_exchange(code)
        except Exception as e:
            print(f"{label} listing failed: {e}", file=sys.stderr)
            continue
        print(f"Yahoo lists {len(quotes)} {label} stocks (ordinary shares).")
        rows += [(q["symbol"], q.get("shortName") or q.get("longName") or q["symbol"], label) for q in quotes]
    with conn, conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO universe (ticker, name, exchange) VALUES %s
            ON CONFLICT (ticker) DO UPDATE SET last_seen = CURRENT_DATE,
              name = COALESCE(EXCLUDED.name, universe.name)""", rows)
        # your watchlist stocks belong to the universe too
        cur.execute("""INSERT INTO universe (ticker, name, exchange)
                       SELECT ticker, name, 'watchlist' FROM watchlist
                       ON CONFLICT (ticker) DO NOTHING""")
        cur.execute("SELECT COUNT(*) FROM universe")
        total = cur.fetchone()[0]
    print(f"Universe now holds {total} stocks (listings are never removed).")
    return len(rows)


def to_rows(ticker, df):
    out, split = [], False
    for ts, r in df.iterrows():
        if pd.isna(r.get("Close")):
            continue
        if r.get("Stock Splits", 0) not in (0, None) and not pd.isna(r.get("Stock Splits", 0)):
            split = True
        out.append((ticker, ts.date(), float(r["Open"]) if pd.notna(r["Open"]) else None,
                    float(r["High"]) if pd.notna(r["High"]) else None,
                    float(r["Low"]) if pd.notna(r["Low"]) else None, float(r["Close"]),
                    float(r.get("Adj Close", r["Close"])) if pd.notna(r.get("Adj Close", r["Close"])) else None,
                    int(r["Volume"]) if pd.notna(r["Volume"]) else None))
    return out, split


def download(tickers, period):
    """{ticker: (rows, had_split)} via batched Yahoo downloads."""
    got = {}
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        for attempt in (1, 2):
            try:
                df = yf.download(chunk, period=period, interval="1d", group_by="ticker", actions=True,
                                 auto_adjust=False, threads=True, progress=False)
                break
            except Exception as e:
                print(f"  Yahoo error ({e}); waiting a minute", file=sys.stderr)
                time.sleep(60)
                df = None
        if df is not None and not df.empty:
            for t in chunk:
                try:
                    sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                except KeyError:
                    continue
                rows, split = to_rows(t, sub.dropna(how="all"))
                if rows:
                    got[t] = (rows, split)
        done = min(i + CHUNK, len(tickers))
        print(f"  {done}/{len(tickers)} stocks", flush=True)
        time.sleep(PAUSE)
    return got


def save(conn, got, full, trim=False):
    with conn, conn.cursor() as cur:
        for t, (rows, _) in got.items():
            if trim:  # after a consolidation, older un-adjusted rows would no longer match
                cur.execute("DELETE FROM daily_prices WHERE ticker = %s AND trade_date < %s", (t, rows[0][1]))
            execute_values(cur, load_history.UPSERT_SQL, rows, page_size=2000)
            if full:
                cur.execute("UPDATE universe SET history_at = now(), rows = %s WHERE ticker = %s", (len(rows), t))


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--backfill", action="store_true")
    g.add_argument("--daily", action="store_true")
    g.add_argument("--refresh", action="store_true")
    ap.add_argument("--years", type=int, default=10)
    args = ap.parse_args()

    conn = psycopg2.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(load_history.CREATE_SQL)
            cur.execute(CREATE_SQL)

        if args.refresh or args.backfill:
            refresh(conn)

        with conn.cursor() as cur:
            if args.backfill:  # resumable, and later runs only load new listings
                cur.execute("SELECT ticker FROM universe WHERE history_at IS NULL ORDER BY ticker")
            else:
                cur.execute("SELECT ticker FROM universe ORDER BY ticker")
            tickers = [r[0] for r in cur.fetchall()]

        if args.backfill:
            start = time.time()
            print(f"Loading {args.years} years of history for {len(tickers)} stocks...")
            for i in range(0, len(tickers), 200):  # save as we go, so a stop loses little
                got = download(tickers[i:i + 200], f"{args.years}y")
                save(conn, got, full=True)
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*), COUNT(DISTINCT ticker), MIN(trade_date) FROM daily_prices "
                            "WHERE ticker IN (SELECT ticker FROM universe)")
                n, k, first = cur.fetchone()
            print(f"Done in {(time.time() - start) / 60:.0f} min: {n:,} daily rows for {k} stocks, back to {first}.")
        elif args.daily:
            got = download(tickers, "5d")
            split = [t for t, (_, s) in got.items() if s]
            save(conn, {t: v for t, v in got.items() if t not in split}, full=False)
            if split:  # consolidation: reload the whole history so it stays comparable
                print(f"Consolidations/splits: {', '.join(split)}; reloading their history")
                save(conn, download(split, f"{args.years}y"), full=True, trim=True)
            print(f"Universe daily update: {len(got)} stocks.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
