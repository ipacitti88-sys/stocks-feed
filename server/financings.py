#!/usr/bin/env python3
"""
Dilution warning: share sales (private placements, bought deals, offerings) found in
each stock's headlines.

Selling new shares is the most common way Venture companies raise money, and it
dilutes existing holders; a company that does it every few months is a drag on returns.
news.py passes every headline it fetches through record(), which keeps the ones that
announce, close or upsize an equity raise in the `financings` table (kept for good,
so the count builds up over time). For the stocks that matter most (my list, the
weekly picks, today's flagged stocks and ideas) the morning run also searches
Google News for "<company> private placement OR financing OR offering" over 6 months.

Several headlines about one raise (announced, upsized, closed) count as one
financing when they're within 21 days of each other.

    python3 financings.py              # stocks with a financing in the last 90 days
    python3 financings.py LVG.V        # one stock's financing headlines
"""
from __future__ import annotations

import datetime as dt
import re
import sys

import psycopg2

import ideas  # about(): does a headline name this company?

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS financings (
    ticker    TEXT NOT NULL,
    key       TEXT NOT NULL,           -- normalised headline, so the same story isn't counted twice
    title     TEXT NOT NULL,
    url       TEXT,
    source    TEXT,
    published DATE,
    found_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, key)
);
"""

# an equity raise: these phrases rarely mean anything else in a small-cap headline
FIN_RE = re.compile(
    r"private placement|bought deal|flow[- ]through|non[- ]brokered|\bbrokered\b|"
    r"\b(?:public|prospectus|unit|equity|marketed|life|listed issuer|share|common share)s? (?:offering|financing)|"
    r"\boffering of (?:units|common shares|shares)|\bat[- ]the[- ]market\b|\bATM (?:program|equity)|"
    r"shares? for debt|\b(?:first|second|third|final) tranche\b|"
    r"\b(?:announces|closes|completes|upsizes?|increases|launches|prices|arranges|proposed)\b[^.]{0,40}\b(?:financing|offering|placement)",
    re.I)
# not a share sale (or not the company's own)
NOT_RE = re.compile(r"debt financing|loan|credit facility|project financing|royalty|streaming|grant|"
                    r"\bfunds? (?:received|from)\b|class action", re.I)
CLUSTER_DAYS = 21
WINDOW_DAYS = 90


def is_financing(title: str) -> bool:
    return bool(title) and bool(FIN_RE.search(title)) and not NOT_RE.search(title)


def ensure(conn):
    with conn, conn.cursor() as cur:
        cur.execute(CREATE_SQL)


def _key(title: str) -> str:
    return re.sub(r"\W+", "", title.lower())[:80]


def _date(when) -> dt.date | None:
    if not when:
        return None
    try:
        return dt.datetime.fromisoformat(str(when).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def record(conn, ticker: str, name: str, items: list[dict]) -> int:
    """Keep the financing headlines among a stock's news items. Returns how many were new."""
    rows = []
    for it in items or []:
        title = str(it.get("title") or "").strip()
        if not is_financing(title) or not ideas.about(title, ticker, name):
            continue
        rows.append((ticker, _key(title), title[:300], it.get("url"), it.get("source"), _date(it.get("time"))))
    if not rows:
        return 0
    n = 0
    with conn, conn.cursor() as cur:
        for r in rows:
            cur.execute("INSERT INTO financings (ticker, key, title, url, source, published) "
                        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (ticker, key) DO NOTHING", r)
            n += cur.rowcount
    return n


def events(heads: list[dict]) -> list[dict]:
    """Group a stock's financing headlines (newest first) into separate raises."""
    out = []
    for h in sorted(heads, key=lambda x: x["d"] or "", reverse=True):
        if not h["d"]:
            continue
        if out and (dt.date.fromisoformat(out[-1]["first"]) - dt.date.fromisoformat(h["d"])).days <= CLUSTER_DAYS:
            out[-1]["first"] = h["d"]
            out[-1]["n"] += 1
            continue
        out.append({"last": h["d"], "first": h["d"], "n": 1, "title": h["title"], "url": h["url"]})
    return out


def summary(conn, today: dt.date | None = None) -> dict:
    """{ticker: {"n90", "n180", "last", "title", "url", "src"}} for stocks with a raise in the last 180 days."""
    today = today or dt.date.today()
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('financings') IS NOT NULL")
        if not cur.fetchone()[0]:
            return {}
        cur.execute("SELECT ticker, title, url, source, published FROM financings "
                    "WHERE published >= %s ORDER BY ticker, published DESC", (today - dt.timedelta(days=180),))
        by = {}
        for t, title, url, src, d in cur.fetchall():
            by.setdefault(t, []).append({"title": title, "url": url, "src": src, "d": d.isoformat() if d else None})
    out = {}
    for t, heads in by.items():
        ev = events(heads)
        if not ev:
            continue
        n90 = sum(1 for e in ev if (today - dt.date.fromisoformat(e["last"])).days <= WINDOW_DAYS)
        out[t] = {"n90": n90, "n180": len(ev), "last": ev[0]["last"], "title": ev[0]["title"][:160],
                  "url": ev[0]["url"], "src": heads[0]["src"]}
    return out


def main() -> int:
    conn = psycopg2.connect()
    try:
        ensure(conn)
        if len(sys.argv) > 1:
            with conn.cursor() as cur:
                for t in sys.argv[1:]:
                    cur.execute("SELECT published, title FROM financings WHERE ticker = %s ORDER BY published DESC NULLS LAST",
                                (t.upper(),))
                    rows = cur.fetchall()
                    print(f"{t.upper()}: {len(rows)} financing headline(s)")
                    for d, title in rows:
                        print(f"  {d or '?':10}  {title}")
            return 0
        s = summary(conn)
        recent = sorted(((v["n90"], t, v) for t, v in s.items() if v["n90"]), reverse=True)
        print(f"{len(recent)} stocks raised money in the last {WINDOW_DAYS} days ({len(s)} in 180 days).")
        for n, t, v in recent:
            print(f"  {t:10} {n} in 90d, latest {v['last']}: {v['title'][:90]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
