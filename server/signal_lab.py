#!/usr/bin/env python3
"""
Signal lab: test buy signals on the price history in the database, the
realistic way, and publish the results to the trading terminal.

For every signal:
  - buy at the NEXT day's open (the scan runs after the close)
  - pay one price step of spread plus 0.5% for fees and slippage
  - try 16 exits: hold 1/5/10/20 days, with or without a 15% stop,
    with or without a 25% profit target (checked on closing prices)
  - compare with simply buying ANY liquid tracked stock that day (the baseline),
    so a rising market doesn't make a signal look good
  - split the period in half: a real edge should show up in both halves

The Rising signal is replayed from saved 5-minute bars (bars_5m) once there
are enough days of them; see intraday_archive.py.

    python3 signal_lab.py               # TSX Venture: writes signal_lab.json to stocks-feed
    python3 signal_lab.py --market tsx  # TSX: writes signal_lab_tsx.json
    python3 signal_lab.py --no-push    # just print
"""
import argparse
import datetime as dt
import json
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import psycopg2

MIN_LIQ = 50_000
MIN_PRICE, MAX_PRICE = 0.10, 5.00
MIN_HISTORY = 60
DEDUPE_ROWS = 5            # same stock, same signal: ignore repeats within 5 trading days
FEE = 0.005                # fees + slippage per round trip, on top of the spread
HOLDS = (1, 5, 10, 20)
STOPS = (0.0, 0.15)
TARGETS = (0.0, 0.25)
MIN_N_HALF = 30
ET = ZoneInfo("America/Toronto")

SIGNALS = {
    "flagged": ("Flagged (current rule)",
                "2+ of: above 50-day average, volume over 2x normal, 52-week high"),
    "breakout": ("Breakout from a quiet base",
                 "new 60-day high after a tight 20-day range (under 30%), volume over 2x normal"),
    "volume_up": ("Volume spike, strong close",
                  "up 5%+ on 3x normal volume, closing in the top quarter of the day's range"),
    "pullback": ("Pullback in an uptrend",
                 "20-day avg above 50-day avg, 12%+ off the 10-day high, first up day on light volume"),
    "gap_hold": ("Gap up that holds",
                 "opens 5%+ above yesterday's close and closes at or above the open, volume over 2x normal"),
    "washout": ("Bounce after a sharp drop",
                "down 20%+ over 5 days, then an up day on above-normal volume"),
}


def tick(p):
    return np.where(p < 0.5, 0.005, 0.01)


def use_universe(conn):
    """True once the research universe (every Venture listing, 10 years) has been loaded."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('universe') IS NOT NULL")
        if not cur.fetchone()[0]:
            return False
        cur.execute("SELECT COUNT(*) FROM universe WHERE history_at IS NOT NULL")
        return cur.fetchone()[0] >= 500


def load_daily(conn, since, universe=False):
    src = ("SELECT ticker, name FROM universe UNION SELECT ticker, name FROM watchlist WHERE active"
           if universe else "SELECT ticker, name FROM watchlist WHERE active")
    q = f"""SELECT d.ticker, COALESCE(MAX(u.name), d.ticker) AS name, d.trade_date, d.open, d.high, d.low,
                  d.close, d.volume
           FROM daily_prices d JOIN ({src}) u ON u.ticker = d.ticker
           WHERE d.trade_date >= %s
           GROUP BY d.ticker, d.trade_date, d.open, d.high, d.low, d.close, d.volume
           ORDER BY d.ticker, d.trade_date"""
    with conn.cursor() as cur:
        cur.execute(q, (since,))
        rows = cur.fetchall()
        cur.execute("SELECT ticker FROM watchlist WHERE active")
        tracked = {r[0] for r in cur.fetchall()}
    df = pd.DataFrame(rows, columns=["ticker", "name", "date", "open", "high", "low", "close", "volume"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["tracked"] = df["ticker"].isin(tracked)
    return df


def add_indicators(df):
    g = df.groupby("ticker", sort=False)
    df["rn"] = g.cumcount() + 1
    df["prev"] = g["close"].shift(1)
    roll = lambda s, n, f: g[s].transform(lambda x: getattr(x.rolling(n, min_periods=n), f)())
    df["ma20"] = roll("close", 20, "mean")
    df["ma50"] = roll("close", 50, "mean")
    df["av20"] = g["volume"].transform(lambda x: x.shift(1).rolling(20, min_periods=20).mean())
    # average dollars traded per day over the previous 20 days
    df["dv20"] = (df["close"] * df["volume"]).groupby(df["ticker"], sort=False).transform(
        lambda x: x.shift(1).rolling(20, min_periods=20).mean())
    df["hi252"] = g["close"].transform(lambda x: x.rolling(252, min_periods=1).max())
    df["hi60p"] = g["close"].transform(lambda x: x.shift(1).rolling(60, min_periods=60).max())
    hi20 = g["high"].transform(lambda x: x.shift(1).rolling(20, min_periods=20).max())
    lo20 = g["low"].transform(lambda x: x.shift(1).rolling(20, min_periods=20).min())
    df["range20"] = (hi20 - lo20) / df["close"]
    df["max10"] = g["close"].transform(lambda x: x.rolling(10, min_periods=10).max())
    df["close5"] = g["close"].shift(5)
    # what happens after: next day's open, then the next 20 closes
    df["entry"] = g["open"].shift(-1)
    df["entry"] = df["entry"].where(df["entry"] > 0, g["close"].shift(-1))
    for k in range(1, max(HOLDS) + 1):
        df[f"f{k}"] = g["close"].shift(-k)
    return df


def signal_masks(df):
    vr = df["volume"] / df["av20"]
    day_range = (df["high"] - df["low"]).replace(0, np.nan)
    up = df["close"] > df["prev"]
    m = {}
    m["flagged"] = ((df["close"] > df["ma50"]).astype(int) + (vr > 2).astype(int)
                    + (df["close"] >= df["hi252"]).astype(int)) >= 2
    m["breakout"] = (df["close"] >= df["hi60p"]) & (df["range20"] <= 0.30) & (vr >= 2)
    m["volume_up"] = (df["close"] / df["prev"] - 1 >= 0.05) & (vr >= 3) \
        & ((df["close"] - df["low"]) / day_range >= 0.75)
    m["pullback"] = (df["ma20"] > df["ma50"]) & (df["close"] > df["ma50"]) \
        & (df["close"] <= 0.88 * df["max10"]) & up & (vr < 1)
    m["gap_hold"] = (df["open"] >= 1.05 * df["prev"]) & (df["close"] >= df["open"]) & (vr >= 2)
    m["washout"] = (df["close"] / df["close5"] - 1 <= -0.20) & up & (vr >= 1)
    return {k: v.fillna(False) for k, v in m.items()}


def dedupe(df, mask):
    idx = np.flatnonzero(mask.to_numpy())
    keep, last_t, last_rn = [], None, -99
    tick_col, rn_col = df["ticker"].to_numpy(), df["rn"].to_numpy()
    for i in idx:
        if tick_col[i] == last_t and rn_col[i] - last_rn <= DEDUPE_ROWS:
            continue
        keep.append(i)
        last_t, last_rn = tick_col[i], rn_col[i]
    out = np.zeros(len(df), bool)
    out[keep] = True
    return out


def simulate(entry, path, hold, stop, target):
    """Net return per trade. path: closes after entry day, first column = entry day's close."""
    p = path[:, :hold]
    ok = ~np.isnan(p[:, -1]) & (entry > 0)
    entry, p = entry[ok], p[ok]
    rel = p / entry[:, None] - 1
    hit = np.zeros_like(rel, bool)
    if stop:
        hit |= rel <= -stop
    if target:
        hit |= rel >= target
    first = np.where(hit.any(1), hit.argmax(1), hold - 1)
    r = rel[np.arange(len(rel)), first] - tick(entry) / entry - FEE
    return ok, r


def stats(r, halves):
    if len(r) == 0:
        return None
    h1, h2 = r[~halves], r[halves]
    f = lambda a: round(float(a.mean()) * 100, 2) if len(a) else None
    return {"n": int(len(r)), "won": round(float((r > 0).mean()) * 100), "avg": f(r),
            "median": round(float(np.median(r)) * 100, 2), "h1": f(h1), "h2": f(h2),
            "n1": int(len(h1)), "n2": int(len(h2))}


def exit_label(hold, stop, target):
    s = f"hold {hold}d"
    if stop:
        s += f", {stop:.0%} stop"
    if target:
        s += f", {target:.0%} target"
    return s


BANDS = [("all", "All prices", MIN_PRICE, MAX_PRICE),
         ("p50", "C$0.50 and up", 0.50, MAX_PRICE),
         ("sub50", "Under C$0.50", MIN_PRICE, 0.4999)]


LIQ_TIERS = (10_000, 25_000, 50_000, 100_000, 250_000)


def compact(results):
    """Just each signal's verdict and best exit, for the liquidity comparison."""
    out = []
    for r in results:
        b = r["best"]
        out.append({k: b.get(k) for k in ("grade", "exit", "avg", "h1", "h2", "edge1", "edge2", "won", "n")}
                   | {"key": r["key"], "name": r["name"], "signals": r["signals"]})
    return out


SCANNER_DOLLARS = None  # set per market in main(): the scanner's own liquidity rule


def run_daily(df, start, until, lo=MIN_PRICE, hi=MAX_PRICE, min_dollar=None):
    if min_dollar == "shares" or (min_dollar is None and not SCANNER_DOLLARS):
        liquid = df["av20"] >= MIN_LIQ
    else:
        liquid = df["dv20"] >= (min_dollar or SCANNER_DOLLARS)
    elig = (df["date"] >= start) & (df["date"] < until) & (df["rn"] >= MIN_HISTORY) \
        & liquid & df["close"].between(lo, hi)
    run_daily.per_day = float(elig.groupby(df["date"]).sum().loc[lambda x: x > 0].mean() or 0)
    mid = start + (until - start) / 2
    paths = df[[f"f{k}" for k in range(1, max(HOLDS) + 1)]].to_numpy(float)
    entry_all = df["entry"].to_numpy(float)
    second = (df["date"] >= mid).to_numpy()

    exits = [(h, s, t) for h in HOLDS for s in STOPS for t in TARGETS]
    base = {}
    for h, s, t in exits:
        sel = elig.to_numpy()
        ok, r = simulate(entry_all[sel], paths[sel], h, s, t)
        base[exit_label(h, s, t)] = stats(r, second[sel][ok])

    results = []
    latest_day = df.loc[df["date"] < until, "date"].max()
    for key, mask in signal_masks(df).items():
        sel = dedupe(df, mask & elig)
        rows = []
        for h, s, t in exits:
            ok, r = simulate(entry_all[sel], paths[sel], h, s, t)
            st = stats(r, second[sel][ok])
            if not st:
                continue
            b = base[exit_label(h, s, t)]
            st["exit"] = exit_label(h, s, t)
            st["edge"] = round(st["avg"] - b["avg"], 2)
            st["edge1"] = round(st["h1"] - b["h1"], 2) if st["h1"] is not None and b["h1"] is not None else None
            st["edge2"] = round(st["h2"] - b["h2"], 2) if st["h2"] is not None and b["h2"] is not None else None
            rows.append(st)
        sig = df[sel]
        recent = sig.sort_values("date").tail(12)
        results.append({
            "key": key, "name": SIGNALS[key][0], "rule": SIGNALS[key][1],
            "signals": int(sel.sum()),
            "exits": rows,
            "best": verdict(rows),
            "recent": [{"date": d.isoformat(), "ticker": t, "name": n, "close": round(float(c), 4)}
                       for d, t, n, c in recent[["date", "ticker", "name", "close"]].itertuples(index=False)],
        })
    # signals on the latest day, from every rule (the "today" list if a rule earns trust)
    today = []
    for key, mask in signal_masks(df).items():
        hit = df[mask & df["tracked"] & (df["date"] == latest_day)
                 & (df["dv20"] >= (SCANNER_DOLLARS or 0)) & (df["av20"] >= (0 if SCANNER_DOLLARS else MIN_LIQ))
                 & df["close"].between(MIN_PRICE, MAX_PRICE) & (df["rn"] >= MIN_HISTORY)]
        today += [{"signal": key, "ticker": t, "name": n, "close": round(float(c), 4)}
                  for t, n, c in hit[["ticker", "name", "close"]].itertuples(index=False)]
    base_list = [dict(exit=k, **v) for k, v in base.items() if v]
    return results, base_list, mid, latest_day, today


def verdict(rows):
    """Pick the best exit and grade it. Picking the best of 16 flatters it, so the bar is high."""
    good = [r for r in rows if r["n1"] >= MIN_N_HALF and r["n2"] >= MIN_N_HALF]
    if not good:
        return {"grade": "Too few signals", "detail": "Not enough signals in each half of the year to judge."}
    b = max(good, key=lambda r: min(r["h1"] or -99, r["h2"] or -99))
    both_profit = (b["h1"] or 0) > 0 and (b["h2"] or 0) > 0
    both_edge = (b["edge1"] or 0) > 0 and (b["edge2"] or 0) > 0
    if both_profit and both_edge:
        grade = "Promising"
        detail = "Made money after costs in both halves of the year and beat the baseline in both. Worth paper trading."
    elif both_edge:
        grade = "Beats the market, still loses"
        detail = "Did better than buying any stock, but still lost money after costs in at least one half."
    elif (b["avg"] or 0) > 0:
        grade = "Inconsistent"
        detail = "Made money overall, but not in both halves. Likely luck or a good stretch for small stocks."
    else:
        grade = "No edge"
        detail = "Lost money after costs with every exit tried."
    return dict(b, grade=grade, detail=detail)


# ---------- Rising, replayed from saved 5-minute bars ----------

def run_rising(conn, daily, keep=lambda t: True):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('bars_5m')")
        if cur.fetchone()[0] is None:
            return {"status": "No 5-minute history saved yet."}
        cur.execute("SELECT ticker, ts, open, close, volume FROM bars_5m ORDER BY ticker, ts")
        rows = cur.fetchall()
    if not rows:
        return {"status": "No 5-minute history saved yet."}
    b = pd.DataFrame(rows, columns=["ticker", "ts", "open", "close", "volume"])
    b = b[b["ticker"].map(keep)]
    b["ts"] = pd.to_datetime(b["ts"], utc=True).dt.tz_convert(ET)
    b["date"] = b["ts"].dt.date
    b["mins"] = (b["ts"].dt.hour * 60 + b["ts"].dt.minute) - (9 * 60 + 30)
    b = b[(b["mins"] >= 0) & (b["mins"] < 390)]
    for c in ("open", "close", "volume"):
        b[c] = pd.to_numeric(b[c], errors="coerce")
    days = sorted(b["date"].unique())
    if len(days) < 15:
        return {"status": f"Only {len(days)} days of 5-minute history so far; need at least 15."}

    d = daily.set_index(["ticker", "date"])
    trades, base = [], []
    checkpoints = range(30, 390, 30)  # 10:00, 10:30, ... 15:30 ET (the 30-minute scan)
    for (t, day), g in b.groupby(["ticker", "date"], sort=False):
        if (t, day) not in d.index:
            continue
        row = d.loc[(t, day)]
        prev, av20 = row["prev"], row["av20"]
        if not (pd.notna(prev) and pd.notna(av20) and av20 >= MIN_LIQ and MIN_PRICE <= prev <= MAX_PRICE):
            continue
        fut = [row["close"]] + [row[f"f{k}"] for k in (1, 5)]  # same-day close, next day, 5 days
        mins, close, vol = g["mins"].to_numpy(), g["close"].to_numpy(float), g["volume"].fillna(0).to_numpy(float)
        cumv = np.cumsum(vol)
        noon = np.searchsorted(mins, 150)  # baseline: buy any liquid stock at noon
        if noon < len(mins):
            base.append((day, close[noon], fut))
        for cp in checkpoints:
            seen = np.searchsorted(mins, cp - 15)       # Yahoo is ~15 minutes behind
            if seen < 1:
                continue
            last = close[seen - 1]
            ref = np.searchsorted(mins, mins[seen - 1] - 30)
            ref30 = close[ref] if ref < seen - 1 else g["open"].iloc[0]
            elapsed = max(mins[seen - 1] + 5, 1)
            pace = cumv[seen - 1] / (av20 * max(elapsed / 390, 0.08))
            if last / prev - 1 >= 0.03 and last > ref30 and pace >= 1.5:
                now = np.searchsorted(mins, cp)          # price when you'd actually buy
                if now < len(mins):
                    trades.append((day, close[now], fut))
                break
    if not trades:
        return {"status": "No Rising signals found in the saved history."}

    mid = days[len(days) // 2]

    def summarize(items, k):
        e = np.array([x[1] for x in items], float)
        x = np.array([float(it[2][k]) if it[2][k] is not None else np.nan for it in items])
        ok = ~np.isnan(x) & (e > 0)
        r = x[ok] / e[ok] - 1 - tick(e[ok]) / e[ok] - FEE
        halves = np.array([it[0] >= mid for it in items])[ok]
        return stats(r, halves)

    out = {"status": "ok", "days": len(days), "from": days[0].isoformat(), "until": days[-1].isoformat(),
           "signals": len(trades), "exits": []}
    for k, label in enumerate(("sell at the close", "sell next day", "hold 5 days")):
        s, bs = summarize(trades, k), summarize(base, k)
        if s and bs:
            s.update(exit=label, edge=round(s["avg"] - bs["avg"], 2), base=bs["avg"])
            out["exits"].append(s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None,
                    help="how far back to test (default: 10 years with the research universe, else 1 year)")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--market", choices=["tsxv", "tsx"], default="tsxv")
    args = ap.parse_args()
    until = dt.date.today()
    import benchmarks
    M = benchmarks.MARKETS[args.market]

    conn = psycopg2.connect()
    try:
        universe = use_universe(conn)
        if args.days is None:
            args.days = 3652 if universe else 365
        start = until - dt.timedelta(days=args.days)
        global SCANNER_DOLLARS
        SCANNER_DOLLARS = benchmarks.MIN_DOLLARS[args.market]
        raw = load_daily(conn, start - dt.timedelta(days=150), universe)
        raw = raw[raw["ticker"].map(lambda t: benchmarks.in_market(t, args.market))].reset_index(drop=True)
        df = add_indicators(raw)
        results, base, mid, latest, today = run_daily(df, start, until)
        old = run_daily(df, start, until, min_dollar="shares")[0]
        liquidity = [{"key": "shares50k", "label": "50,000+ shares a day (old rule)",
                      "stocks_per_day": round(run_daily.per_day), "signals": compact(old)}]
        for amt in LIQ_TIERS:
            r = run_daily(df, start, until, min_dollar=amt)[0]
            liquidity.append({"key": f"d{amt}", "label": f"C${amt // 1000}k+ traded a day",
                              "min_dollar": amt, "stocks_per_day": round(run_daily.per_day), "signals": compact(r)})
        bands = []
        for key, label, lo, hi in BANDS:
            r, b = (results, base) if key == "all" else run_daily(df, start, until, lo, hi)[:2]
            bands.append({"key": key, "label": label, "signals": r, "baseline": b})
        rising = run_rising(conn, df, lambda t: benchmarks.in_market(t, args.market))
        market = None
        try:
            ser = benchmarks.history(conn, days=args.days + 30).get(M["index"], [])
            def chg(a, b):
                x = [c for d, c in ser if d < a]
                y = [c for d, c in ser if d < b]
                return round((y[-1] / x[-1] - 1) * 100, 1) if x and y else None
            market = {"name": benchmarks.BENCHMARKS[M["index"]], "full": chg(start, until),
                      "h1": chg(start, mid), "h2": chg(mid, until)}
        except Exception as e:
            print(f"market comparison skipped: {e}", file=sys.stderr)
    finally:
        conn.close()

    out = {
        "generated": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
        "from": start.isoformat(), "until": until.isoformat(), "split": mid.isoformat(),
        "market_key": args.market, "market_label": M["label"],
        "liquidity_rule": f"C${SCANNER_DOLLARS // 1000}k+ traded a day (your scanner's rule)",
        "universe": (f"every {M['label']} listing (incl. ones that later stopped trading)" if universe
                     else f"{M['label']} stocks on your watchlist today"),
        "stocks": int(df["ticker"].nunique()),
        "latest_day": latest.isoformat() if latest is not None else None,
        "method": ("Buy at the next day's open; costs = one price step of spread + 0.5%. "
                   "Baseline = buying any liquid tracked stock that day with the same exit. "
                   "Half 1 / half 2 = the two halves of the period."),
        "baseline": base, "signals": results, "bands": bands, "liquidity": liquidity, "market": market, "rising": rising, "today": today,
    }

    print(f"Signal lab, {M['label']}: {start} to {until} (halves split {mid})")
    if market and market["full"] is not None:
        print(f" Market: {market['name']} {market['full']:+.1f}% over the period "
              f"(half 1 {market['h1']:+.1f}%, half 2 {market['h2']:+.1f}%)")
    print(" Liquidity test (all prices; avg per trade after costs, best exit; * = Promising):")
    for lv in liquidity:
        cells = "  ".join(f"{x['key'][:9]:>9} {('-' if x['avg'] is None else format(x['avg'], '+.1f'))}{'*' if x['grade'] == 'Promising' else ' '}"
                          for x in lv["signals"])
        print(f"  {lv['label']:38} ~{lv['stocks_per_day']:4} stocks/day | {cells}")
    for band in bands:
        print(f" {band['label']}:")
        for r in band["signals"]:
            b = r["best"]
            line = f"  {r['name']:30} {r['signals']:5} signals  {b['grade']}"
            if "exit" in b:
                line += (f"  | best: {b['exit']}: avg {b['avg']:+.1f}%  halves {b['h1']:+.1f}% / {b['h2']:+.1f}%"
                         f"  vs baseline {b['edge']:+.1f}%")
            print(line)
    if rising.get("status") == "ok":
        print(f"  Rising (5-minute replay, {rising['days']} days): {rising['signals']} signals")
        for e in rising["exits"]:
            print(f"     {e['exit']:18} avg {e['avg']:+.1f}%  won {e['won']}%  vs baseline {e['edge']:+.1f}%")
    else:
        print(f"  Rising: {rising.get('status')}")

    if args.no_push:
        return 0
    import export_feed
    feed = export_feed.FEED_DIR
    if not (feed / ".git").exists():
        print("No stocks-feed repo; wrote nothing.", file=sys.stderr)
        return 1
    export_feed.git("pull", "--rebase", "--quiet")
    (feed / f"signal_lab{M['file']}.json").write_text(json.dumps(out, separators=(",", ":"), default=str))
    export_feed.git("add", f"signal_lab{M['file']}.json")
    if export_feed.git("diff", "--cached", "--quiet").returncode != 0:
        export_feed.git("commit", "-q", "-m", "signal lab " + out["generated"])
        r = export_feed.git("push", "-q")
        if r.returncode != 0:
            print("Push failed:\n" + r.stderr, file=sys.stderr)
            return 1
    print(f"signal_lab{M['file']}.json pushed to stocks-feed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
