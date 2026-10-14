#!/usr/bin/env python3
"""
Discover TSX Venture stocks and add them to the watchlist.

Asks Yahoo's screener for every TSXV stock that is:
  - priced between MIN_PRICE and MAX_PRICE
  - trading at least MIN_AVG_VOLUME shares a day (3-month average)
New tickers are added to `watchlist` (source = 'tsxv-scan') and get one
year of price history loaded, so the daily screen can rate them right away.

    python3 discover.py            # scan and add new names
    python3 discover.py --dry-run  # just show what would be added
"""
import argparse
import sys
import time

import psycopg2
import yfinance as yf

import load_history

MIN_PRICE = 0.10          # skip sub-dime names (wide spreads, very risky)
MAX_PRICE = 5.00
MIN_AVG_VOLUME = 50_000  # shares/day, so you can get in and out


def scan() -> list[dict]:
    try:
        from yfinance import EquityQuery
    except ImportError:
        sys.exit("This yfinance is too old for the screener. Run: ./venv/bin/pip install -U yfinance")

    q = EquityQuery("and", [
        EquityQuery("eq", ["exchange", "VAN"]),          # VAN = TSX Venture on Yahoo
        EquityQuery("gt", ["intradayprice", MIN_PRICE]),
        EquityQuery("lt", ["intradayprice", MAX_PRICE]),
        EquityQuery("gt", ["avgdailyvol3m", MIN_AVG_VOLUME]),
    ])

    found, offset = [], 0
    while True:
        res = yf.screen(q, offset=offset, size=250, sortField="avgdailyvol3m", sortAsc=False)
        quotes = res.get("quotes", [])
        found += quotes
        total = res.get("total", len(found))
        offset += len(quotes)
        if not quotes or offset >= total:
            break
        time.sleep(1)  # be polite to Yahoo between pages
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    quotes = scan()
    print(f"Yahoo returned {len(quotes)} TSXV stocks between ${MIN_PRICE} and ${MAX_PRICE} "
          f"with {MIN_AVG_VOLUME:,}+ avg daily volume")

    conn = psycopg2.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(load_history.CREATE_SQL)
            cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'manual'")
            cur.execute("SELECT ticker FROM watchlist")
            known = {r[0] for r in cur.fetchall()}

        new = [q for q in quotes if q.get("symbol") and q["symbol"] not in known]
        print(f"{len(new)} new to your watchlist")
        if args.dry_run:
            for q in new:
                print(f"  {q['symbol']:10} {q.get('regularMarketPrice', '?'):>8}  {q.get('shortName', '')}")
            return 0

        for q in new:
            ticker = q["symbol"]
            name = q.get("shortName") or q.get("longName") or ticker
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO watchlist (ticker, name, source) VALUES (%s, %s, 'tsxv-scan') "
                    "ON CONFLICT (ticker) DO NOTHING",
                    (ticker, name),
                )
            load_history.load(conn, ticker, name, "1y")
            time.sleep(0.5)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
