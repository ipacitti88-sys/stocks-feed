#!/usr/bin/env python3
"""
Which analysis works best? One scoreboard for every source of stock calls, all
measured the same way on real prices:

  Scanner       Flagged, Breakout, Rising (the terminal's signal log)
  Ideas         the Dashboard's ranked ideas, by tier (logged daily from now on)
  Weekly picks  Strong candidate, Watch, and the names the research rejected
  Pre-market    the 6:53am note's verdict on each stock: Backed, Unclear, Caution
  Reddit        Canadian names in the nightly meme review
  News          stocks right after a share-sale (financing) headline

Each call is "bought" at the open of the first trading day after it was made (the
same morning for the pre-market note) and valued at the close 5, 10 and 20 trading
days later. Results are shown two ways:
  net       after the stock's estimated spread (costs.py) plus 0.5% fees
  vs stock  the gain beyond the average liquid tracked stock in the same market over
            the same days, before costs. Positive = the analysis picked better than
            chance; this is the fairest way to compare sources.
The same stock called again by the same source within 5 trading days counts once.
Under 20 calls a source is marked "too early": small samples swing a lot.

    python3 scoreboard.py
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys

import numpy as np
import pandas as pd
import psycopg2

import costs

HORIZONS = (5, 10, 20)
FEE = 0.005
MIN_N = 20
TICKER = r"[A-Z0-9][A-Z0-9-]{0,9}\.(?:V|TO|CN|NE)"
VERDICT_RE = re.compile(r"\b(Backed|Unclear|Caution)\b\s*\.?\s*$", re.I)

SOURCES = [  # key, group, label, called after the close/evening (True) or before the open (False)
    ("sig_flagged", "Scanner", "Flagged", True),
    ("sig_breakout", "Scanner", "Breakout", True),
    ("sig_rising", "Scanner", "Rising", True),
    ("idea_strong", "Ideas", "Strongest evidence", True),
    ("idea_some", "Ideas", "Some evidence", True),
    ("idea_weak", "Ideas", "Weak evidence", True),
    ("pick_strong", "Weekly picks", "Strong candidate", True),
    ("pick_watch", "Weekly picks", "Watch", True),
    ("pick_rejected", "Weekly picks", "Rejected", True),
    ("am_backed", "Pre-market note", "Backed", False),
    ("am_unclear", "Pre-market note", "Unclear", False),
    ("am_caution", "Pre-market note", "Caution", False),
    ("meme_ca", "Reddit", "Canadian names on Reddit", True),
    ("fin", "News", "After a share-sale headline", True),
]

IDEAS_LOG_SQL = """
CREATE TABLE IF NOT EXISTS ideas_log (
    day    DATE NOT NULL,
    ticker TEXT NOT NULL,
    tier   TEXT,
    rank   INT,
    score  NUMERIC(6,2),
    top    TEXT,
    PRIMARY KEY (day, ticker)
);
"""


def log_ideas(conn, ideas: dict) -> None:
    """Keep each day's ranked ideas (dashboard.py calls this) so they can be scored later."""
    rows = []
    for mkt in ("tsxv", "tsx"):
        m = (ideas or {}).get(mkt) or {}
        day = m.get("day")
        for n, i in enumerate(m.get("ideas") or [], 1):
            if day and i.get("t"):
                top = (i.get("signals") or [{}])[0].get("key")
                rows.append((day, i["t"], i.get("tier"), n, i.get("score"), top))
    with conn, conn.cursor() as cur:
        cur.execute(IDEAS_LOG_SQL)
        for r in rows:
            cur.execute("INSERT INTO ideas_log (day, ticker, tier, rank, score, top) VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (day, ticker) DO NOTHING", r)


def _docs(cur, coll):
    cur.execute("SELECT DISTINCT ON (doc_id) doc_id, data FROM terminal_backup WHERE collection = %s "
                "ORDER BY doc_id, (source = 'terminal') DESC", (coll,))
    for i, d in cur.fetchall():
        d = d if isinstance(d, dict) else json.loads(d)
        yield str(d.get("date") or i)[:10], d


def collect_calls(conn) -> list[tuple]:
    """[(source key, ticker, date the call was made)]."""
    calls = []
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('terminal_backup') IS NOT NULL")
        if cur.fetchone()[0]:
            for day, d in _docs(cur, "signals"):
                for e in d.get("entries") or []:
                    if e.get("type") in ("flagged", "breakout", "rising"):
                        calls.append(("sig_" + e["type"], e.get("ticker"), day))
            for day, d in _docs(cur, "weeks"):
                for p in d.get("picks") or []:
                    calls.append(("pick_strong" if p.get("rating") == "strong" else "pick_watch", p.get("ticker"), day))
                for r in d.get("rejected") or []:
                    calls.append(("pick_rejected", r.get("ticker"), day))
            for day, d in _docs(cur, "mornings"):
                for line in str(d.get("text") or "").splitlines():
                    m = VERDICT_RE.search(line.strip())
                    if not m or len(line) > 400:
                        continue
                    key = "am_" + m.group(1).lower()
                    # the tickers the line is about: those before the first dash (the verdict's subject)
                    head = re.split(r"\s[—–-]\s", line, maxsplit=1)[0]
                    for t in re.findall(TICKER, head.upper()):
                        calls.append((key, t, day))
            for day, d in _docs(cur, "memes"):
                seen = set()
                for k in ("top", "movers", "canadian", "penny"):
                    for r in d.get(k) or []:
                        ex = r.get("ex") or []
                        ex = ex if isinstance(ex, list) else [ex]
                        base = str(r.get("ticker") or "").upper().split(".")[0]
                        suf = ".V" if "TSXV" in ex else ".TO" if "TSX" in ex else ".CN" if "CSE" in ex else None
                        if suf and base and base not in seen:
                            seen.add(base)
                            calls.append(("meme_ca", base + suf, day))
        cur.execute("SELECT to_regclass('ideas_log') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("SELECT day, ticker, tier FROM ideas_log")
            for day, t, tier in cur.fetchall():
                if tier in ("strong", "some", "weak"):
                    calls.append(("idea_" + tier, t, day.isoformat()))
        cur.execute("SELECT to_regclass('financings') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("SELECT ticker, published FROM financings WHERE published IS NOT NULL")
            for t, day in cur.fetchall():
                calls.append(("fin", t, day.isoformat()))
    ok = re.compile(f"^{TICKER}$")
    return [(k, str(t).upper(), d) for k, t, d in calls if t and ok.match(str(t).upper()) and d]


def compute(conn) -> dict:
    calls = collect_calls(conn)
    if not calls:
        return {"asOf": None, "sources": [], "calls": 0}
    first = min(d for _, _, d in calls)
    since = dt.date.fromisoformat(first) - dt.timedelta(days=120)
    import benchmarks
    with conn.cursor() as cur:
        cur.execute("""SELECT d.ticker, d.trade_date, d.open, d.high, d.low, d.close, d.volume FROM daily_prices d
                       WHERE d.trade_date >= %s AND (d.ticker = ANY(%s)
                         OR d.ticker IN (SELECT ticker FROM watchlist WHERE active))
                       ORDER BY d.ticker, d.trade_date""", (since, sorted({t for _, t, _ in calls})))
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["ticker", "date", "open", "high", "low", "close", "volume"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    if df.empty:
        return {"asOf": None, "sources": [], "calls": len(calls)}
    df = costs.add_spread(df)
    g = df.groupby("ticker", sort=False)
    df["dv20"] = (df["close"] * df["volume"]).groupby(df["ticker"], sort=False).transform(
        lambda x: x.shift(1).rolling(20, min_periods=10).mean())
    df["entry"] = df["open"].where(df["open"] > 0, df["close"])
    for h in HORIZONS:
        df[f"r{h}"] = g["close"].shift(-(h - 1)) / df["entry"] - 1
    df["date"] = df["date"].astype(str)

    # the average liquid tracked stock, per market and entry day: the "chance" baseline
    liquid = df["dv20"] >= df["ticker"].map(benchmarks.min_dollars)
    df["mkt"] = np.where(df["ticker"].str.endswith(".TO"), "tsx", "tsxv")
    base = {h: df[liquid].groupby(["mkt", "date"])[f"r{h}"].mean() for h in HORIZONS}

    by_t = {t: x.reset_index(drop=True) for t, x in df.groupby("ticker", sort=False)}
    after = {k: a for k, _, _, a in SOURCES}
    seen, trades = {}, []
    for key, t, day in sorted(calls, key=lambda c: c[2]):
        x = by_t.get(t)
        if x is None:
            continue
        dates = x["date"].to_numpy()
        i = int(np.searchsorted(dates, day, side="right" if after[key] else "left"))
        if i >= len(x):
            continue
        last = seen.get((key, t))
        if last is not None and i - last <= 5:
            continue
        seen[(key, t)] = i
        row = x.iloc[i]
        spr = float(x.iloc[i - 1]["spr"]) if i > 0 else float(row["spr"])   # known before the entry
        cost = max(spr, (0.005 if row["entry"] < 0.5 else 0.01) / row["entry"]) + FEE
        tr = {"key": key, "t": t, "day": day, "entry_day": row["date"], "mkt": row["mkt"]}
        for h in HORIZONS:
            r = row[f"r{h}"]
            if pd.notna(r):
                b = base[h].get((row["mkt"], row["date"]))
                tr[h] = (float(r), float(r) - cost, float(r - b) if b is not None and pd.notna(b) else None)
        trades.append(tr)

    out = []
    for key, group, label, _ in SOURCES:
        T = [x for x in trades if x["key"] == key]
        s = {"key": key, "group": group, "label": label, "calls": len(T), "h": {}}
        for h in HORIZONS:
            v = [x[h] for x in T if h in x]
            if not v:
                continue
            gross = np.array([a for a, _, _ in v])
            net = np.array([b for _, b, _ in v])
            ex = np.array([c for _, _, c in v if c is not None])
            s["h"][h] = {"n": len(v), "won": round(float((net > 0).mean()) * 100),
                         "net": round(float(net.mean()) * 100, 2), "gross": round(float(gross.mean()) * 100, 2),
                         "ex": round(float(ex.mean()) * 100, 2) if len(ex) else None,
                         # rough 95% range of the average edge: +/- 2 standard errors
                         "exSe": round(float(ex.std(ddof=1) / np.sqrt(len(ex))) * 200, 2) if len(ex) > 2 else None}
        out.append(s)

    def verdict(s, h=10):
        x = s["h"].get(h) or s["h"].get(5)
        if not x or x["n"] < MIN_N or x["ex"] is None:
            return "early"
        if x["exSe"] is not None and x["ex"] - x["exSe"] > 0:
            return "ahead"
        if x["exSe"] is not None and x["ex"] + x["exSe"] < 0:
            return "behind"
        return "unclear"
    for s in out:
        s["verdict"] = verdict(s)
    ranked = sorted((s for s in out if s["verdict"] != "early"), key=lambda s: -((s["h"].get(10) or s["h"].get(5))["ex"]))
    return {"asOf": df["date"].max(), "calls": len(trades), "sources": out,
            "best": ranked[0]["key"] if ranked and ranked[0]["verdict"] == "ahead" else None,
            "horizons": list(HORIZONS), "minN": MIN_N}


def main() -> int:
    conn = psycopg2.connect()
    try:
        r = compute(conn)
    finally:
        conn.close()
    print(f"{r['calls']} scored calls, prices to {r['asOf']}. Avg gain beyond the average stock (vs stock) and after costs (net):")
    for s in r["sources"]:
        cells = "  ".join(f"{h}d: n={x['n']:<3} vs stock {x['ex'] if x['ex'] is not None else '-':>6}%  net {x['net']:+.1f}%"
                          for h, x in s["h"].items())
        print(f"  {s['group'] + ' · ' + s['label']:44} {s['verdict']:8} {cells or 'no results yet'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
