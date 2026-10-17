#!/usr/bin/env python3
"""
Discover TSX Venture and TSX stocks and add them to the watchlist.

Asks Yahoo's screener for every TSXV stock that is:
  - priced between MIN_PRICE and MAX_PRICE
  - trading at least MIN_AVG_VOLUME shares a day (3-month average), or enough
    dollars a day for the scanner's liquidity rule (see benchmarks.MIN_DOLLARS)
New tickers are added to `watchlist` (source = 'tsxv-scan') and get one
year of price history loaded, so the daily screen can rate them right away.

    python3 discover.py            # scan and add new names
    python3 discover.py --dry-run  # just show what would be added
"""
import argparse
import re
import sys
import time

import psycopg2
import yfinance as yf

import load_history

MIN_PRICE = 0.10          # skip sub-dime names (wide spreads, very risky)
MAX_PRICE = 5.00
MIN_AVG_VOLUME = 50_000  # shares/day, so you can get in and out
SCREEN_VOLUME = 10_000   # ask Yahoo for anything above this, then apply the rules below


def worth_tracking(q) -> bool:
    """50,000+ shares a day, OR enough dollars traded for the scanner (C$50k a day, both markets)."""
    import benchmarks
    vol, px = q.get("averageDailyVolume3Month"), q.get("regularMarketPrice")
    if vol is None or px is None:  # Yahoo left the fields out: keep it, the daily screen still applies the rule
        return True
    return vol >= MIN_AVG_VOLUME or benchmarks.liquid_enough(q.get("symbol", ""), vol, px)


EXCHANGES = {"VAN": ("TSX Venture", "tsxv-scan"), "TOR": ("TSX", "tsx-scan")}
# not ordinary shares: preferreds, debentures, warrants, rights, notes, subscription receipts
NOT_COMMON = re.compile(r"-(P[A-Z0-9]*|PR[A-Z0-9-]*|DB[A-Z0-9]*|WT[A-Z0-9]*|R|RT|NT[A-Z0-9]*|IR|SR)\.(TO|V)$")


def is_common(q) -> bool:
    sym = q.get("symbol") or ""
    if q.get("quoteType") not in (None, "EQUITY"):
        return False
    return not NOT_COMMON.search(sym)


def scan(exchange="VAN") -> list[dict]:
    try:
        from yfinance import EquityQuery
    except ImportError:
        sys.exit("This yfinance is too old for the screener. Run: ./venv/bin/pip install -U yfinance")

    q = EquityQuery("and", [
        EquityQuery("eq", ["exchange", exchange]),       # VAN = TSX Venture, TOR = TSX
        EquityQuery("gt", ["intradayprice", MIN_PRICE]),
        EquityQuery("lt", ["intradayprice", MAX_PRICE]),
        EquityQuery("gt", ["avgdailyvol3m", SCREEN_VOLUME]),
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
    return [q for q in found if is_common(q) and worth_tracking(q)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    quotes = []
    for code, (label, source) in EXCHANGES.items():
        try:
            found = scan(code)
        except Exception as e:  # one exchange failing shouldn't stop the other
            print(f"{label} scan failed: {e}", file=sys.stderr)
            continue
        for q in found:
            q["_source"] = source
        quotes += found
        print(f"Yahoo returned {len(found)} {label} stocks between ${MIN_PRICE} and ${MAX_PRICE} "
              f"trading {MIN_AVG_VOLUME:,}+ shares or enough dollars a day")
        time.sleep(1)

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
                    "INSERT INTO watchlist (ticker, name, source) VALUES (%s, %s, %s) "
                    "ON CONFLICT (ticker) DO NOTHING",
                    (ticker, name, q["_source"]),
                )
            load_history.load(conn, ticker, name, "1y")
            time.sleep(0.5)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
