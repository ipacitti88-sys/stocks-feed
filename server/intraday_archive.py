#!/usr/bin/env python3
"""
Save every tracked stock's 5-minute bars for good, and write them out as
spreadsheet files (CSV) you can open in Excel or Google Sheets.

Yahoo only keeps 5-minute data for about 60 days, so this runs after the
close each weekday and keeps a permanent copy in the bars_5m table. That
history is what lets us test intraday signals like "Rising" properly.

    python3 intraday_archive.py              # last 5 days (what the daily job runs)
    python3 intraday_archive.py --backfill   # everything Yahoo still has (~60 days)

Spreadsheets: one file per trading day in $WEB_DIR/intraday/, e.g.
    http://192.168.1.108:8080/stocks/intraday/
Columns: date, time (Eastern), ticker, name, open, high, low, close, volume
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
import yfinance as yf

ET = ZoneInfo("America/Toronto")
CHUNK = 100

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS bars_5m (
    ticker TEXT NOT NULL,
    ts     TIMESTAMPTZ NOT NULL,
    open   NUMERIC(14,4),
    high   NUMERIC(14,4),
    low    NUMERIC(14,4),
    close  NUMERIC(14,4),
    volume BIGINT,
    PRIMARY KEY (ticker, ts)
);
"""

UPSERT_SQL = """
INSERT INTO bars_5m (ticker, ts, open, high, low, close, volume) VALUES %s
ON CONFLICT (ticker, ts) DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high,
  low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume
"""


def num(v, n=4):
    return None if v is None or pd.isna(v) else round(float(v), n)


def fetch(tickers: list[str], days: int) -> list[tuple]:
    rows = []
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        try:
            df = yf.download(chunk, period=f"{days}d", interval="5m", group_by="ticker",
                             auto_adjust=False, threads=True, progress=False)
        except Exception as e:  # Yahoo hiccup: skip this chunk, keep going
            print(f"download failed for {chunk[0]}..: {e}", file=sys.stderr)
            continue
        if df is None or df.empty:
            continue
        for t in chunk:
            try:
                sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            sub = sub.dropna(subset=["Close"])
            for ts, r in sub.iterrows():
                ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
                vol = int(r["Volume"]) if pd.notna(r["Volume"]) else None
                rows.append((t, ts.to_pydatetime(), num(r["Open"]), num(r["High"]),
                             num(r["Low"]), num(r["Close"]), vol))
        time.sleep(1)
    return rows


def write_csvs(rows: list[tuple], names: dict, out: Path) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    by_day = defaultdict(list)
    for t, ts, o, h, l, c, v in rows:
        local = ts.astimezone(ET)
        by_day[local.date().isoformat()].append((t, local.strftime("%H:%M"), o, h, l, c, v))
    written = []
    for day, items in sorted(by_day.items()):
        items.sort(key=lambda x: (x[0], x[1]))
        path = out / f"{day}.csv"
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "time_et", "ticker", "name", "open", "high", "low", "close", "volume"])
            for t, hm, o, h, l, c, v in items:
                w.writerow([day, hm, t, names.get(t, t), o, h, l, c, v])
        written.append(day)
    write_index(out)
    return written


def write_index(out: Path) -> None:
    files = sorted(out.glob("????-??-??.csv"), reverse=True)
    items = "\n".join(
        f'<li><a href="{f.name}" download>{f.stem}</a> <span>{f.stat().st_size / 1e6:.1f} MB</span></li>'
        for f in files)
    (out / "index.html").write_text(f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>5-minute data</title>
<style>body{{font:15px system-ui,sans-serif;margin:24px auto;max-width:640px;padding:0 16px;color:#222}}
li{{margin:6px 0}} span{{color:#888;font-size:13px;margin-left:8px}}</style>
<h1>5-minute price data</h1>
<p>One spreadsheet (CSV) per trading day, every tracked stock. Times are Eastern.
Open in Excel, Numbers or Google Sheets (File &rarr; Import).</p>
<ul>{items or "<li>Nothing saved yet.</li>"}</ul>
<p><a href="../">&larr; Back to the dashboard</a></p>
""")


def day_bars(conn, first: dt.date, last: dt.date, tickers=None) -> dict:
    """{date: {ticker: {"t0": epoch seconds of first bar, "m": minutes after t0, "c": closes, "v": volumes}}}
    for regular-session bars (9:30-16:00 Eastern) between two dates."""
    q = ("SELECT ticker, ts, close, volume FROM bars_5m "
         "WHERE ts >= %s AND ts < %s" + (" AND ticker = ANY(%s)" if tickers else "") + " ORDER BY ticker, ts")
    a = dt.datetime.combine(first, dt.time(0), ET)
    b = dt.datetime.combine(last + dt.timedelta(days=1), dt.time(0), ET)
    with conn.cursor() as cur:
        cur.execute(q, (a, b, list(tickers)) if tickers else (a, b))
        rows = cur.fetchall()
    out = {}
    for t, ts, c, v in rows:
        if c is None:
            continue
        loc = ts.astimezone(ET)
        mins = loc.hour * 60 + loc.minute
        if mins < 570 or mins >= 960:  # outside the regular session
            continue
        d = out.setdefault(loc.date().isoformat(), {})
        e = d.get(t)
        epoch = int(ts.timestamp())
        if e is None:
            e = d[t] = {"t0": epoch, "m": [], "c": [], "v": []}
        e["m"].append((epoch - e["t0"]) // 60)
        e["c"].append(round(float(c), 4))
        e["v"].append(int(v) if v is not None else 0)
    return out


def write_day_json(conn, out: Path, dates=None) -> list[str]:
    """One JSON file per trading day (all stocks) for the server webpage's 1-day chart, plus days.json."""
    out.mkdir(parents=True, exist_ok=True)
    with conn.cursor() as cur:
        cur.execute("SELECT MIN(ts), MAX(ts) FROM bars_5m")
        lo, hi = cur.fetchone()
    if lo is None:
        return []
    if dates:
        first, last = min(dates), max(dates)
    else:
        first, last = lo.astimezone(ET).date(), hi.astimezone(ET).date()
    written = []
    d0 = first
    while d0 <= last:  # a week at a time keeps memory small
        d1 = min(d0 + dt.timedelta(days=6), last)
        for day, stocks in day_bars(conn, d0, d1).items():
            (out / f"{day}.json").write_text(json.dumps({"date": day, "s": stocks}, separators=(",", ":")))
            written.append(day)
        d0 = d1 + dt.timedelta(days=1)
    days = sorted(f.stem for f in out.glob("????-??-??.json"))
    (out / "days.json").write_text(json.dumps(days))
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="fetch the ~60 days Yahoo still has")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--export-all", action="store_true",
                    help="only rewrite the 1-day chart files for every archived day")
    args = ap.parse_args()
    days = 59 if args.backfill else args.days
    web = Path(os.environ.get("WEB_DIR", Path(__file__).parent / "web")) / "intraday"
    if args.export_all:
        conn = psycopg2.connect()
        try:
            n = write_day_json(conn, web)
        finally:
            conn.close()
        print(f"1-day chart files written for {len(n)} trading days in {web}/")
        return 0

    conn = psycopg2.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            cur.execute("SELECT ticker, COALESCE(name, ticker) FROM watchlist WHERE active ORDER BY ticker")
            names = dict(cur.fetchall())

        rows = fetch(sorted(names), days)
        if not rows:
            print("No 5-minute data returned (holiday or Yahoo issue).")
            return 0
        with conn, conn.cursor() as cur:
            execute_values(cur, UPSERT_SQL, rows, page_size=2000)
            cur.execute("SELECT COUNT(DISTINCT ts::date), COUNT(*) FROM bars_5m")
            n_days, n_rows = cur.fetchone()
        written = write_csvs(rows, names, web)
        write_day_json(conn, web, [dt.date.fromisoformat(d) for d in written])
    finally:
        conn.close()

    tickers = len({r[0] for r in rows})
    print(f"Saved {len(rows):,} five-minute bars for {tickers} stocks "
          f"({written[0]} to {written[-1]}). Archive now holds {n_rows:,} bars over {n_days} days.")
    print(f"Spreadsheets: {web}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
