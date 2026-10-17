#!/usr/bin/env python3
"""
Ideas for the webpage's Dashboard tab: stocks worth a closer look at the next session,
ranked by how well each one's signals held up in the signal lab, plus context from
news, the terminal's morning notes, weekly picks and meme reviews.

The lab (signal_lab.py, every Saturday) tests six buy signals over 10 years, per market
and price band. This scores each stock that fired a signal at the last close by that
signal's verdict for the stock's own market and price band, then adds context.

It's the evidence laid out in one place, not a forecast: no signal has a reliable edge
for a single day once costs are counted; the tested edges played out over 5 to 20 days.

    python3 ideas.py          # print the current ideas for both markets
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

GRADE_SCORE = {"Promising": 3.0, "Beats the market, still loses": 1.0, "Inconsistent": 0.5,
               "Too few signals": 0.0, "No edge": -1.0}
NAMES = {"flagged": "Flagged", "breakout": "Breakout", "volume_up": "Volume surge", "pullback": "Pullback",
         "gap_hold": "Gap up that held", "washout": "Bounce after a selloff"}
HERE = Path(__file__).resolve().parent
FEED_DIR = Path(os.environ.get("FEED_DIR", Path.home() / "stocks-feed"))
WEB_DIR = Path(os.environ.get("WEB_DIR", HERE / "web"))
TOP_N = 10
FUNDS = re.compile(r"\b(FUND|ETF|ETFS|SPLIT CORP|CLOSED-END)\b", re.I)  # funds, not companies: skip


def market_of(t):
    return "tsx" if t.endswith(".TO") else "tsxv"


def load_lab():
    out = {}
    for mkt, f in (("tsxv", "signal_lab.json"), ("tsx", "signal_lab_tsx.json")):
        try:
            out[mkt] = json.loads((FEED_DIR / f).read_text())
        except (OSError, ValueError):
            out[mkt] = None
    return out


def grade_of(lab, key, price):
    """(band label, best-exit dict) for a signal at this price, from the lab's price bands."""
    if not lab:
        return None, {}
    bands = {b["key"]: b for b in lab.get("bands") or []}
    band = bands.get("sub50" if price < 0.5 else "p50") or bands.get("all")
    sigs = (band or {}).get("signals") or lab.get("signals") or []
    s = next((x for x in sigs if x.get("key") == key), None)
    if not s:
        return None, {}
    b = dict(s.get("best") or {})
    b["n"] = s.get("signals")
    return (band or {}).get("label", "All prices"), b


def one_day(lab):
    """Average result of buying each signal and selling the next day, after costs (all prices)."""
    out = {}
    for s in (lab or {}).get("signals") or []:
        x = next((e for e in s.get("exits") or [] if e.get("exit") == "hold 1d"), None)
        if x:
            out[s["key"]] = {"avg": x.get("avg"), "won": x.get("won"), "n": x.get("n")}
    return out


def todays_hits(conn):
    """Every signal that fired at the latest close on a tracked, liquid stock: {ticker: info}."""
    import benchmarks
    import signal_lab as sl
    df = sl.add_indicators(sl.load_daily(conn, dt.date.today() - dt.timedelta(days=420)))
    masks = sl.signal_masks(df)
    out = {}
    for mkt in ("tsxv", "tsx"):
        sub = df["ticker"].str.endswith(".TO") if mkt == "tsx" else ~df["ticker"].str.endswith(".TO")
        if not sub.any():
            continue
        day = df.loc[sub, "date"].max()
        base = (sub & df["tracked"] & (df["date"] == day) & df["close"].between(sl.MIN_PRICE, sl.MAX_PRICE)
                & (df["dv20"] >= benchmarks.MIN_DOLLARS[mkt]) & (df["rn"] >= sl.MIN_HISTORY))
        for key, m in masks.items():
            for r in df[base & m].itertuples(index=False):
                e = out.setdefault(r.ticker, {"t": r.ticker, "n": r.name, "c": round(float(r.close), 4),
                                              "date": str(r.date), "dv": round(float(r.dv20)),
                                              "chg": round((r.close / r.prev - 1) * 100, 1) if r.prev else None,
                                              "rv": round(float(r.volume / r.av20), 1) if r.av20 else None,
                                              "sig": []})
                e["sig"].append(key)
    return out


GENERIC = {"THE", "INC", "LTD", "CORP", "CORPORATION", "COMPANY", "CO", "LIMITED", "GROUP", "HOLDINGS", "RESOURCES",
           "RESOURCE", "METALS", "MINING", "MINES", "MINERALS", "GOLD", "SILVER", "COPPER", "ENERGY", "EXPLORATION",
           "TECHNOLOGIES", "TECHNOLOGY", "CAPITAL", "INTERNATIONAL", "CANADA", "CANADIAN", "NORTH", "AMERICAN", "NEW",
           "VENTURES", "INDUSTRIES", "OIL", "GAS", "DATA", "SOLUTIONS", "SYSTEMS", "THERAPEUTICS", "BIOSCIENCES"}


def about(title, t, name):
    """True when a headline names the company (a distinctive word of its name) or its ticker."""
    T = " " + re.sub(r"[^A-Z0-9]+", " ", str(title).upper()) + " "
    base = t.split(".")[0]
    if re.search(r"(TSXV|TSX|CVE|TSX-V|V)\s*:?\s*" + re.escape(base) + r"\b", str(title).upper()) or (len(base) >= 4 and f" {base} " in T):
        return True
    words = [w for w in re.sub(r"[^A-Z0-9 ]+", " ", str(name or "").upper()).split() if w not in GENERIC and len(w) >= 3]
    return bool(words) and f" {words[0]} " in T


def recent_news(t, days=3, name=None):
    try:
        j = json.loads((WEB_DIR / "news" / f"{t}.json").read_text())
    except (OSError, ValueError):
        return None
    name = name or j.get("name")
    cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    for it in j.get("items") or []:
        try:
            when = dt.datetime.fromisoformat(str(it.get("time")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo and when >= cut and it.get("title") and about(it["title"], t, name):
            return {"title": str(it["title"])[:160], "url": it.get("url"), "time": it.get("time"),
                    "source": it.get("source")}
    return None


def mentions(term, t):
    """What the terminal's notes, picks and meme reviews said about a ticker lately."""
    good, bad, notes = 0.0, [], []
    if not term:
        return good, bad, notes
    base = re.escape(t.split(".")[0])
    rx = re.compile(r"(^|[^A-Z0-9])" + base + r"(\.(V|TO|CN|NE))?([^A-Z0-9]|$)")
    for m in (term.get("mornings") or [])[:5]:
        for line in str(m.get("text") or "").splitlines():
            if len(line) < 300 and rx.search(line.upper()) and not re.match(r"(?i)\s*(pre-market|full note)", line):
                txt = line.strip(" -•")
                notes.append({"d": m.get("date") or m.get("id"), "src": "Pre-market note", "txt": txt})
                if re.search(r"\bcaution\b", txt, re.I):
                    bad.append("Pre-market note said Caution")
                elif re.search(r"\bbacked\b", txt, re.I):
                    good += 0.5
    for w in (term.get("weeks") or [])[:2]:
        for p in w.get("picks") or []:
            if p.get("ticker") == t:
                good += 1.0 if p.get("rating") == "strong" else 0.5
                notes.append({"d": w.get("date") or w.get("id"), "src": "Weekly pick · " + ("strong candidate" if p.get("rating") == "strong" else "watch"),
                              "txt": p.get("verdict") or p.get("catalyst") or ""})
        for r in w.get("rejected") or []:
            if r.get("ticker") == t:
                bad.append("Rejected in weekly picks: " + str(r.get("reason") or "")[:120])
    for n in (term.get("memes") or [])[:2]:
        for k in ("top", "movers", "canadian", "penny"):
            for r in n.get(k) or []:
                ex = r.get("ex") or []
                ex = ex if isinstance(ex, list) else [ex]
                if str(r.get("ticker") or "").upper() == t.split(".")[0] and any(e in ("TSX", "TSXV", "CSE") for e in ex):
                    notes.append({"d": n.get("date") or n.get("id"), "src": "Meme review",
                                  "txt": f"{r.get('mentions', '?')} Reddit mentions {r.get('change') or ''}".strip()})
                    bad.append("Reddit attention: meme moves often reverse")
    return good, bad, notes


def build(hits, lab, term, my=()):
    """Rank the stocks that fired a signal at the last close. Returns {market: payload}."""
    out = {}
    for mkt in ("tsxv", "tsx"):
        L = lab.get(mkt)
        rows = []
        for t, h in hits.items():
            if market_of(t) != mkt or FUNDS.search(str(h.get("n") or "")):
                continue
            sigs = []
            for key in h["sig"]:
                band, b = grade_of(L, key, h["c"])
                sigs.append({"key": key, "name": NAMES.get(key, key), "grade": b.get("grade") or "Not tested yet",
                             "exit": b.get("exit"), "avg": b.get("avg"), "won": b.get("won"), "n": b.get("n"), "band": band})
            sigs.sort(key=lambda s: -GRADE_SCORE.get(s["grade"], 0))
            score = GRADE_SCORE.get(sigs[0]["grade"], 0) if sigs else 0
            score += 0.5 * sum(1 for s in sigs[1:] if s["grade"] == "Promising")
            reasons, warns = [], []
            good, bad, notes = mentions(term, t)
            score += good
            warns += bad
            nw = recent_news(t, name=h.get("n"))
            if nw:
                score += 0.5
            import benchmarks
            if h["dv"] < 2 * benchmarks.MIN_DOLLARS[mkt]:
                warns.append(f"Thin: about C${round(h['dv'] / 1000)}k traded a day")
            score -= 1.0 * sum(1 for w in bad if w.startswith("Rejected"))
            top = sigs[0] if sigs else None
            if top and top["grade"] == "Promising":
                where = {"Under C$0.50": " priced under C$0.50", "C$0.50 and up": " priced C$0.50 and up"}.get(top["band"] or "", "")
                reasons.append(f"{top['name']} signals on {mkt_label(mkt)} stocks{where} averaged {fmt_pct(top['avg'])} a trade "
                               f"over 10 years ({top['exit']}), winning {top['won']}% of the time")
            elif top:
                reasons.append(f"{top['name']}: {top['grade'].lower()} in the 10-year test for this price range")
            rows.append({**h, "signals": sigs, "score": round(score, 2), "reasons": reasons, "warns": warns,
                         "notes": notes[:4], "news": nw, "mine": t in my,
                         "tier": "strong" if score >= 3 else "some" if score >= 1 else "weak"})
        rows.sort(key=lambda r: (-r["score"], -(r["rv"] or 0)))
        out[mkt] = {"day": max((r["date"] for r in rows), default=None), "ideas": rows[:TOP_N],
                    "more": max(0, len(rows) - TOP_N), "oneDay": one_day(L),
                    "lab": lab_summary(L), "labGenerated": (L or {}).get("generated"),
                    "risingTest": {k: v for k, v in ((L or {}).get("rising") or {}).items() if k in ("status", "days", "signals", "exits")}}
    return out


def lab_summary(L):
    if not L:
        return None
    bands = {b["key"]: b for b in L.get("bands") or []}
    rows = []
    for s in L.get("signals") or []:
        r = {"key": s["key"], "name": NAMES.get(s["key"], s.get("name")), "rule": s.get("rule")}
        for bk in ("all", "p50", "sub50"):
            src = next((x for x in (bands.get(bk) or {}).get("signals") or [] if x["key"] == s["key"]), s if bk == "all" else None)
            b = (src or {}).get("best") or {}
            r[bk] = {"grade": b.get("grade"), "avg": b.get("avg"), "exit": b.get("exit"), "won": b.get("won")}
        rows.append(r)
    return {"rows": rows, "from": L.get("from"), "until": L.get("until"), "market": L.get("market")}


def mkt_label(m):
    return "TSX" if m == "tsx" else "Venture"


def fmt_pct(v):
    return "–" if v is None else f"{v:+.1f}%"


def compute(conn, term=None, my=()):
    """Ideas for both markets. Signals only change once a day, so the result is cached."""
    lab = load_lab()
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date), MAX(loaded_at) FROM daily_prices")
        last_day, loaded = cur.fetchone()
    key = json.dumps([str(last_day), str(loaded), [(lab[m] or {}).get("generated") for m in ("tsxv", "tsx")]])
    cache = WEB_DIR / ".ideas_cache.json"
    hits = None
    try:
        c = json.loads(cache.read_text())
        if c.get("key") == key:
            hits = c["hits"]
    except (OSError, ValueError, KeyError):
        pass
    if hits is None:
        hits = todays_hits(conn)
        try:
            WEB_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"key": key, "hits": hits}, default=str))
        except OSError:
            pass
    return build(hits, lab, term, set(my))


def main() -> int:
    import psycopg2
    import dashboard
    conn = psycopg2.connect()
    try:
        with conn.cursor() as cur:
            term = dashboard.terminal_records(cur)
        res = compute(conn, term)
    finally:
        conn.close()
    for mkt, r in res.items():
        print(f"== {mkt_label(mkt)} (signals at the {r['day']} close)")
        for i in r["ideas"]:
            print(f"  {i['score']:4}  {i['t']:9} {i['c']:<7} " + ", ".join(f"{s['name']} ({s['grade']})" for s in i["signals"])
                  + (f"  ! {'; '.join(i['warns'])}" if i["warns"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
