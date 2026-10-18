#!/usr/bin/env python3
"""
Build the stock dashboard: one self-contained web page written to web/index.html.

    python3 dashboard.py

run_daily.sh calls this after the screen, and a small web server shares the
web/ folder on your home network (see setup.sh), e.g. http://192.168.1.108:8080
"""
import datetime as dt
import decimal
import json
import os
import sys
from pathlib import Path

import psycopg2

HISTORY_DAYS = 380  # calendar days of price history (covers the 1Y chart)

SCREEN_SQL = """
WITH p AS (
  SELECT d.ticker, d.trade_date, d.close, d.volume,
    AVG(d.close)  OVER w50  AS ma50,
    AVG(d.volume) OVER w20  AS avg_vol20,
    MAX(d.close)  OVER w252 AS high_52w,
    LAG(d.close, 20) OVER (PARTITION BY d.ticker ORDER BY d.trade_date) AS close_20d_ago,
    ROW_NUMBER()  OVER (PARTITION BY d.ticker ORDER BY d.trade_date DESC) AS rn
  FROM daily_prices d
  JOIN watchlist w ON w.ticker = d.ticker AND w.active
  WINDOW
    w50  AS (PARTITION BY d.ticker ORDER BY d.trade_date ROWS 49 PRECEDING),
    w20  AS (PARTITION BY d.ticker ORDER BY d.trade_date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),
    w252 AS (PARTITION BY d.ticker ORDER BY d.trade_date ROWS 251 PRECEDING)
)
SELECT
  p.ticker,
  p.trade_date,
  p.close,
  ROUND(p.volume / NULLIF(p.avg_vol20, 0), 2)                  AS rel_vol,
  ROUND(100 * (p.close / NULLIF(p.close_20d_ago, 0) - 1), 1)   AS pct_20d,
  ROUND(100 * (p.close / NULLIF(p.high_52w, 0) - 1), 1)        AS from_high,
  ROUND(p.avg_vol20)                                           AS avg_vol20,
  p.close > p.ma50                                             AS above_ma50,
  p.volume > 2 * p.avg_vol20                                   AS vol_spike,
  p.close >= p.high_52w                                        AS new_high,
  COALESCE(w.name, p.ticker)                                   AS name,
  w.added_on
FROM p JOIN watchlist w ON w.ticker = p.ticker
WHERE p.rn = 1
"""

HISTORY_SQL = f"""
SELECT ticker, trade_date, close, volume, ma50 FROM (
  SELECT d.ticker, d.trade_date, d.close, d.volume,
    AVG(d.close) OVER (PARTITION BY d.ticker ORDER BY d.trade_date ROWS 49 PRECEDING) AS ma50
  FROM daily_prices d
  JOIN watchlist w ON w.ticker = d.ticker AND w.active
  WHERE d.trade_date >= CURRENT_DATE - {HISTORY_DAYS + 80}
) x
WHERE trade_date >= CURRENT_DATE - {HISTORY_DAYS}
ORDER BY ticker, trade_date
"""

INTRA_BARS_SQL = """
SELECT ticker, ts, close, volume FROM intraday_bars
WHERE ts >= (SELECT date_trunc('day', MAX(ts) AT TIME ZONE 'America/Toronto') AT TIME ZONE 'America/Toronto' FROM intraday_bars)
ORDER BY ticker, ts
"""

LIVE_SQL = """
SELECT ticker, as_of, last, pct_day, pct_30m, pace_rel_vol, day_volume, rising, first_rising_at
FROM intraday_quotes
WHERE trade_date = CURRENT_DATE
"""

STATUS_SQL = """
SELECT
  (SELECT COUNT(*) FROM watchlist WHERE active),
  (SELECT COUNT(*) FROM watchlist WHERE active AND added_on >= CURRENT_DATE - 7),
  (SELECT MAX(loaded_at) FROM daily_prices),
  (SELECT MAX(trade_date) FROM daily_prices)
"""


def is_tsx(t):
    return t.endswith(".TO")


# one page per market, same layout: index.html = TSX Venture, tsx.html = TSX
MARKETS = [("tsxv", "TSX Venture", "index.html", lambda t: not is_tsx(t)),
           ("tsx", "TSX", "tsx.html", is_tsx)]


def num(v):
    if v is None:
        return None
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


def iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None else None)


TERM_LIMITS = {"mornings": 30, "weeks": 12, "memes": 7, "signals": 90, "alertlog": 30}


def terminal_records(cur):
    """The trading terminal's own records, from its latest backup (see terminal_backup.py)."""
    cur.execute("SELECT to_regclass('terminal_backup') IS NOT NULL")
    if not cur.fetchone()[0]:
        return None
    out = {}
    for coll, n in TERM_LIMITS.items():
        cur.execute("SELECT doc_id, data FROM terminal_backup WHERE source = 'terminal' AND collection = %s "
                    "ORDER BY COALESCE(data->>'date', doc_id) DESC LIMIT %s", (coll, n))
        out[coll] = [dict(d if isinstance(d, dict) else json.loads(d), id=i) for i, d in cur.fetchall()]
    cur.execute("SELECT data FROM terminal_backup WHERE source = 'terminal' AND collection = 'paper' AND doc_id = 'account'")
    r = cur.fetchone()
    out["paper"] = (r[0] if isinstance(r[0], dict) else json.loads(r[0])) if r else None
    cur.execute("SELECT MAX(changed_at), MAX(first_seen) FROM terminal_backup WHERE source = 'terminal'")
    a, b = cur.fetchone()
    out["copied"] = iso(max([x for x in (a, b) if x], default=None))
    return out


PICK_HORIZONS = (5, 10, 20)  # trading days held: about 1, 2 and 4 weeks


def picks_scorecard(conn) -> dict | None:
    """How every weekly pick did: bought at the open of the first trading day after the
    Sunday shortlist, then the close 5, 10 and 20 trading days in (and the latest close),
    next to the market index over the same days (TSX Venture for .V, TSX for .TO)."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('terminal_backup') IS NOT NULL")
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT DISTINCT ON (doc_id) doc_id, data FROM terminal_backup WHERE collection = 'weeks' "
                    "ORDER BY doc_id, (source = 'terminal') DESC")
        weeks = [(i, d if isinstance(d, dict) else json.loads(d)) for i, d in cur.fetchall()]
        picks = []
        for wid, w in weeks:
            day = str(w.get("date") or wid)[:10]
            try:
                dt.date.fromisoformat(day)
            except ValueError:
                continue
            seen = set()
            for p in w.get("picks") or []:
                t = str((p or {}).get("ticker") or "").strip().upper()
                if not t or t in seen or not t.split(".")[-1] in ("V", "TO", "CN", "NE"):
                    continue
                seen.add(t)
                picks.append({"w": day, "t": t, "n": str(p.get("name") or t)[:80],
                              "r": "strong" if p.get("rating") == "strong" else "watch", "rank": p.get("rank")})
        if not picks:
            return {"rows": [], "asOf": None}
        first = min(p["w"] for p in picks)
        tickers = sorted({p["t"] for p in picks} | {"^SPCDNX", "^GSPTSE"})
        cur.execute("SELECT ticker, trade_date, open, close FROM daily_prices WHERE ticker = ANY(%s) "
                    "AND trade_date > %s ORDER BY ticker, trade_date", (tickers, first))
        px = {}
        for t, d, o, c in cur.fetchall():
            if c is None:
                continue
            px.setdefault(t, []).append((d.isoformat(), num(o) or num(c), num(c)))

    def path(t, after):
        """[(date, open, close)] from the first trading day after `after`."""
        return [r for r in px.get(t, []) if r[0] > after]

    rows, as_of = [], None
    for p in picks:
        s = path(p["t"], p["w"])
        ix = path("^GSPTSE" if p["t"].endswith(".TO") else "^SPCDNX", p["w"])
        row = dict(p, e=None, ed=None, days=0)
        if s:
            e = s[0][1]
            row.update(e=round(e, 4), ed=s[0][0], days=len(s), last=round(s[-1][2] / e - 1, 4))
            ixe = ix[0][1] if ix and ix[0][0] == s[0][0] else None
            if ixe:
                row["ixl"] = round(ix[min(len(s), len(ix)) - 1][2] / ixe - 1, 4)
            for h in PICK_HORIZONS:
                if len(s) >= h:
                    row[f"h{h}"] = round(s[h - 1][2] / e - 1, 4)
                    if ixe and len(ix) >= h:
                        row[f"x{h}"] = round(ix[h - 1][2] / ixe - 1, 4)
            as_of = max(as_of or s[-1][0], s[-1][0])
        rows.append(row)
    rows.sort(key=lambda r: (r["w"], -(r["rank"] if isinstance(r["rank"], (int, float)) else 99)), reverse=True)
    return {"rows": rows, "asOf": as_of, "horizons": list(PICK_HORIZONS)}


def scoreboard_cached(conn):
    """The scoreboard only changes with new daily prices or new terminal records: cache it."""
    import scoreboard
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date), MAX(loaded_at)::date FROM daily_prices")
        key = [str(x) for x in cur.fetchone()]
        for sql in ("SELECT MAX(changed_at), COUNT(*) FROM terminal_backup", "SELECT COUNT(*) FROM ideas_log",
                    "SELECT COUNT(*) FROM financings"):
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (sql.split("FROM ")[1],))
            if cur.fetchone()[0]:
                cur.execute(sql)
                key += [str(x) for x in cur.fetchone()]
    cache = Path(os.environ.get("WEB_DIR", Path(__file__).parent / "web")) / ".scoreboard_cache.json"
    try:
        c = json.loads(cache.read_text())
        if c.get("key") == key:
            return c["score"]
    except (OSError, ValueError, KeyError):
        pass
    score = scoreboard.compute(conn)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": key, "score": score}, default=str))
    except OSError:
        pass
    return score


def suggest_stops(cost: dict) -> dict:
    """Add a suggested stop-loss to each stock: k x its typical daily move below the last close,
    where k is the multiple that did best in the signal lab for its market and price band."""
    try:
        import ideas
        lab = ideas.load_lab()
    except Exception:
        lab = {}
    for t, v in cost.items():
        px, atr = v.get("px"), v.get("atr")
        if not px or not atr:
            continue
        L = lab.get("tsx" if t.endswith(".TO") else "tsxv") or {}
        bands = ((L.get("stops") or {}).get("bands") or {})
        b = bands.get("sub50" if px < 0.5 else "p50") or bands.get("all") or {}
        k = b.get("best_atr") or 2.5
        step = 0.005 if px < 0.5 else 0.01
        lvl = max(px * (1 - min(max(k * atr, 0.03), 0.6)), step)
        v["stop"] = round(int(lvl / step) * step, 3)
        v["stopK"] = k
        v["stopBest"] = b.get("best")
    return cost


def main() -> int:
    conn = psycopg2.connect()
    try:
        try:
            import mylist
            my = mylist.state(conn)
        except Exception as e:
            print(f"my list unavailable: {e}", file=sys.stderr)
            conn.rollback()
            my = None
        try:
            with conn.cursor() as cur:
                term = terminal_records(cur)
        except Exception as e:
            print(f"terminal records unavailable: {e}", file=sys.stderr)
            conn.rollback()
            term = None
        try:  # Dashboard tab: ranked ideas from the signal lab, plus context
            import ideas as ideas_mod
            ideas = ideas_mod.compute(conn, term, [i["t"] for i in (my or {}).get("items", []) if i.get("pending") != "remove"])
        except Exception as e:
            print(f"ideas unavailable: {e}", file=sys.stderr)
            conn.rollback()
            ideas = {}
        try:  # which analysis works best (see scoreboard.py): log today's ideas, then score every source
            import scoreboard
            scoreboard.log_ideas(conn, ideas)
            score = scoreboard_cached(conn)
        except Exception as e:
            print(f"scoreboard unavailable: {e}", file=sys.stderr)
            conn.rollback()
            score = None
        try:  # Track record tab: how the Sunday picks did
            picks = picks_scorecard(conn)
        except Exception as e:
            print(f"picks scorecard unavailable: {e}", file=sys.stderr)
            conn.rollback()
            picks = None
        try:  # dilution warning: recent share sales found in the news (see financings.py)
            import financings
            fin = financings.summary(conn)
        except Exception as e:
            print(f"financings unavailable: {e}", file=sys.stderr)
            conn.rollback()
            fin = {}
        try:  # trading cost, position size and a suggested stop for every stock (see costs.py)
            import costs
            cost = suggest_stops(costs.current(conn))
        except Exception as e:
            print(f"trading costs unavailable: {e}", file=sys.stderr)
            conn.rollback()
            cost = {}
        try:  # phone alerts sent today (see live_alerts.py)
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('live_alerts') IS NOT NULL")
                live_al = []
                if cur.fetchone()[0]:
                    cur.execute("SELECT ticker, kind, level, price, at, sent FROM live_alerts "
                                "WHERE trade_date >= CURRENT_DATE - 7 ORDER BY at DESC LIMIT 40")
                    live_al = [{"t": t, "k": k, "lvl": num(l), "p": num(p), "at": iso(a), "sent": s}
                               for t, k, l, p, a, s in cur.fetchall()]
            import notify
            live_al = {"on": notify.enabled(), "list": live_al}
        except Exception as e:
            print(f"live alerts unavailable: {e}", file=sys.stderr)
            conn.rollback()
            live_al = None
        try:
            import benchmarks
            bench = benchmarks.summary(conn)
        except Exception as e:
            print(f"benchmarks unavailable: {e}", file=sys.stderr)
            conn.rollback()
            bench = {}
        with conn.cursor() as cur:
            cur.execute(SCREEN_SQL)
            screen = cur.fetchall()
            cur.execute(HISTORY_SQL)
            history = cur.fetchall()
            cur.execute(STATUS_SQL)
            tracked, new7, loaded_at, last_day = cur.fetchone()
            cur.execute("SELECT to_regclass('intraday_quotes') IS NOT NULL")
            live = []
            if cur.fetchone()[0]:
                cur.execute(LIVE_SQL)
                live = cur.fetchall()
            cur.execute("SELECT to_regclass('intraday_bars') IS NOT NULL")
            intra_bars = []
            if cur.fetchone()[0]:
                cur.execute(INTRA_BARS_SQL)
                intra_bars = cur.fetchall()
    finally:
        conn.close()

    stocks = []
    for (t, d, close, rv, p20, fh, avgv, ma, spike, nh, name, added) in screen:
        signals = sum(bool(x) for x in (ma, spike, nh))
        stocks.append({
            "t": t, "n": name, "d": iso(d), "c": num(close), "rv": num(rv),
            "p20": num(p20), "fh": num(fh), "av": num(avgv),
            "ma": bool(ma), "vs": bool(spike), "nh": bool(nh),
            "s": signals, "added": iso(added),
        })

    hist = {}
    for t, d, close, vol, ma50 in history:
        h = hist.setdefault(t, {"d": [], "c": [], "v": [], "m": []})
        h["d"].append(iso(d))
        h["c"].append(num(close))
        h["v"].append(int(vol) if vol is not None else None)
        h["m"].append(round(num(ma50), 4) if ma50 is not None else None)

    intra = {}
    for t, ts, close, vol in intra_bars:
        b = intra.setdefault(t, {"d": [], "c": [], "v": []})
        b["d"].append(iso(ts))
        b["c"].append(num(close))
        b["v"].append(int(vol) if vol is not None else None)

    live_by = {}
    live_as_of = None
    for t, as_of, last, pday, p30, pace, dvol, rising, since in live:
        live_by[t] = {"lp": num(last), "pd": num(pday), "p30": num(p30), "lrv": num(pace),
                      "lv": num(dvol), "up": bool(rising), "since": iso(since)}
        live_as_of = max(live_as_of, as_of) if live_as_of else as_of
    for s in stocks:
        if s["t"] in live_by:
            s.update(live_by[s["t"]])

    template = (Path(__file__).parent / "dashboard_template.html").read_text()
    out_dir = Path(os.environ.get("WEB_DIR", Path(__file__).parent / "web"))
    out_dir.mkdir(parents=True, exist_ok=True)
    for key, label, page, pick in MARKETS:
        mine = [s for s in stocks if pick(s["t"])]
        keep = {s["t"] for s in mine}
        data = {
            "market": label,
            "liveAsOf": iso(live_as_of),
            "generated": dt.datetime.now().isoformat(timespec="minutes"),
            "loadedAt": iso(loaded_at),
            "lastDay": iso(last_day),
            "tracked": len(mine),
            "new7": sum(1 for s in mine if s["added"] and s["added"] >= str(dt.date.today() - dt.timedelta(days=7))),
            "minDollars": __import__("benchmarks").MIN_DOLLARS[key],
            "stocks": mine,
            "hist": {t: v for t, v in hist.items() if t in keep},
            "intra": {t: v for t, v in intra.items() if t in keep},
            # shared with the trading terminal: your list, its records, and prices for both markets
            "my": my,
            "ideas": ideas.get(key),
            "bench": (lambda b: b and {**{k: v for k, v in b.items() if k != "dates"}, "closes": b.get("closes", [])[-253:]})(
                (bench or {}).get("^GSPTSE" if key == "tsx" else "^SPCDNX")),
            "term": term,
            "allPx": {s["t"]: [s.get("lp") if s.get("lp") is not None else s["c"], s["c"], s["n"]] for s in stocks},
            "listApiPort": int(os.environ.get("LIST_API_PORT", "8081")),
            "picks": picks,
            "fin": fin,
            "cost": cost,
            "score": score,
            "liveAlerts": live_al,
        }
        nav = '<nav class="mkt">' + "".join(
            f'<a href="{pg}"{" class=on" if k == key else ""}>{lb}</a>' for k, lb, pg, _ in MARKETS) + "</nav>"
        payload = json.dumps(data, separators=(",", ":"), default=lambda o: float(o) if isinstance(o, decimal.Decimal) else str(o))
        payload = payload.replace("</", "<\\/")  # never let data close the <script> tag
        html = template.replace("/*__DATA__*/null", payload).replace("<!--NAV-->", nav)
        (out_dir / page).write_text(html)
        print(f"Dashboard written: {out_dir / page} ({len(mine)} {label} stocks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
