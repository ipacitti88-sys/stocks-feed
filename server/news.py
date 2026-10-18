#!/usr/bin/env python3
"""
Latest news for each tracked stock, for the webpage's stock detail panel.

Sources: Yahoo Finance's news feed for the ticker, topped up from Google News
(search on the company name) when Yahoo has little, which is common for small
Venture companies. Writes one small file per stock: $WEB_DIR/news/<TICKER>.json

    python3 news.py --all    # every tracked stock (morning run, ~10 minutes)
    python3 news.py --hot    # today's rising, flagged and breakout stocks (every 30 min)
    python3 news.py LVG.V PNG.V

Every headline fetched also goes through financings.record(), which keeps share sales
(private placements, offerings) for the dilution warning. The morning --all run also
searches Google News for recent financings of the stocks that matter most (my list,
weekly picks, today's flagged stocks and ideas).
"""
import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import psycopg2
import yfinance as yf

import financings

KEEP = 8                 # headlines per stock
MAX_AGE_DAYS = 90
UA = "Mozilla/5.0 (X11; Linux x86_64) stocks-server"
OUT = Path(os.environ.get("WEB_DIR", Path(__file__).parent / "web")) / "news"


def iso(ts):
    return ts.astimezone(dt.timezone.utc).isoformat(timespec="minutes") if ts else None


def from_yahoo(ticker):
    items = []
    try:
        raw = yf.Ticker(ticker).news or []
    except Exception:
        return items
    for n in raw:
        c = n.get("content") if isinstance(n.get("content"), dict) else n   # newer and older yfinance
        title = c.get("title")
        url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
               or c.get("link"))
        src = (c.get("provider") or {}).get("displayName") or c.get("publisher") or "Yahoo Finance"
        when = None
        if c.get("pubDate"):
            try:
                when = dt.datetime.fromisoformat(c["pubDate"].replace("Z", "+00:00"))
            except ValueError:
                pass
        elif c.get("providerPublishTime"):
            when = dt.datetime.fromtimestamp(c["providerPublishTime"], dt.timezone.utc)
        if title and url:
            items.append({"title": title.strip(), "url": url, "source": src, "time": iso(when),
                          "summary": (c.get("summary") or "")[:240]})
    return items


def clean_name(name):
    n = re.sub(r"\b(inc|corp|corporation|ltd|limited|co|company|plc)\b\.?", "", name or "", flags=re.I)
    return re.sub(r"[^\w&' -]", " ", n).strip()


def from_google(name, extra="", days=MAX_AGE_DAYS):
    q = clean_name(name)
    if len(q) < 3:
        return []
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f'"{q}"{(" " + extra) if extra else ""} when:{days}d', "hl": "en-CA", "gl": "CA", "ceid": "CA:en"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        root = ET.fromstring(urllib.request.urlopen(req, timeout=15).read())
    except Exception:
        return []
    items = []
    for it in root.iter("item"):
        title = html.unescape(it.findtext("title") or "")
        src = it.findtext("source") or "Google News"
        if title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        when = None
        if it.findtext("pubDate"):
            try:
                when = email.utils.parsedate_to_datetime(it.findtext("pubDate"))
            except (TypeError, ValueError):
                pass
        if title and it.findtext("link"):
            items.append({"title": title.strip(), "url": it.findtext("link"), "source": src,
                          "time": iso(when), "summary": ""})
    return items


def news_for(ticker, name, conn=None):
    items = from_yahoo(ticker)
    if len(items) < 3:
        items += from_google(name)
    if conn is not None:  # dilution warning: keep any share-sale headlines, even ones trimmed below
        try:
            financings.record(conn, ticker, name, items)
        except Exception as e:
            conn.rollback()
            print(f"{ticker}: financing check failed: {e}", file=sys.stderr)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=MAX_AGE_DAYS)
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["time"] or "", reverse=True):
        key = re.sub(r"\W+", "", it["title"].lower())[:60]
        if key in seen:
            continue
        if it["time"] and dt.datetime.fromisoformat(it["time"]) < cutoff:
            continue
        seen.add(key)
        out.append(it)
    return out[:KEEP]


def pick(conn, mode, wanted):
    with conn.cursor() as cur:
        cur.execute("SELECT ticker, COALESCE(name, ticker) FROM watchlist WHERE active")
        names = dict(cur.fetchall())
        if mode == "all":
            return names
        if mode == "list":
            return {t: names.get(t, t) for t in wanted}
        hot = set()
        cur.execute("SELECT to_regclass('intraday_quotes') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("SELECT ticker FROM intraday_quotes WHERE trade_date = (SELECT MAX(trade_date) FROM intraday_quotes) "
                        "AND (rising OR pct_day >= 5)")
            hot |= {r[0] for r in cur.fetchall()}
    feed = Path.home() / "stocks-feed" / "latest.json"
    try:
        L = json.loads(feed.read_text())
        hot |= {x["ticker"] for k in ("flagged", "breakouts", "rising") for x in L.get(k) or []}
    except Exception:
        pass
    return {t: names.get(t, t) for t in hot if t in names}


def key_stocks(conn, names):
    """My list, recent weekly picks, today's flagged/breakout stocks and ideas: worth a deeper financing search."""
    want = set()
    with conn.cursor() as cur:
        for sql in ("SELECT ticker FROM my_list",
                    "SELECT p->>'ticker' FROM terminal_backup, jsonb_array_elements(data->'picks') p "
                    "WHERE collection = 'weeks' AND COALESCE(data->>'date', doc_id) >= to_char(CURRENT_DATE - 60, 'YYYY-MM-DD')"):
            try:
                cur.execute(sql)
                want |= {r[0] for r in cur.fetchall() if r[0]}
            except Exception:
                conn.rollback()
    try:
        L = json.loads((Path.home() / "stocks-feed" / "latest.json").read_text())
        want |= {x["ticker"] for k in ("flagged", "breakouts") for x in L.get(k) or []}
        I = json.loads((Path.home() / "stocks-feed" / "ideas.json").read_text())
        for m in (I.get("markets") or I).values() if isinstance(I, dict) else []:
            if isinstance(m, dict):
                want |= {i.get("t") for i in m.get("ideas") or [] if isinstance(i, dict)}
    except Exception:
        pass
    return {t: names[t] for t in want if t in names}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--hot", action="store_true")
    ap.add_argument("tickers", nargs="*")
    args = ap.parse_args()
    mode = "all" if args.all else ("list" if args.tickers else "hot")

    conn = psycopg2.connect()
    try:
        financings.ensure(conn)
        todo = pick(conn, mode, args.tickers)

        OUT.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
        found = 0
        for t, name in sorted(todo.items()):
            items = news_for(t, name, conn)
            found += bool(items)
            (OUT / f"{t}.json").write_text(json.dumps({"ticker": t, "name": name, "fetched": now, "items": items},
                                                      separators=(",", ":")))
            time.sleep(0.6)  # be gentle with Yahoo and Google
        print(f"News updated for {len(todo)} stocks ({found} with recent headlines).")

        if mode == "all":  # once a day: look back 6 months for share sales by the stocks that matter most
            with conn.cursor() as cur:
                cur.execute("SELECT ticker, COALESCE(name, ticker) FROM watchlist WHERE active")
                names = dict(cur.fetchall())
            keys = key_stocks(conn, names)
            new = 0
            for t, name in sorted(keys.items()):
                hits = from_google(name, "(\"private placement\" OR financing OR offering)", 180)
                new += financings.record(conn, t, name, hits)
                time.sleep(0.6)
            print(f"Financing search for {len(keys)} key stocks: {new} new financing headline(s).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
