#!/usr/bin/env python3
"""
Pattern study over the research universe (every TSX Venture listing, up to
10 years). Publishes patterns.json for the trading terminal's Track record tab.

Method, so the results mean something:
  - buy at the NEXT day's open (no buying at a close you only see afterwards)
  - compare with the average stock in the same price band on the same day
    ("excess"), so a rising or falling market doesn't fake a pattern
  - check every calendar year separately: a real pattern shows up in most years
  - only stocks that were tradable at the time: price C$0.05+ and 20,000+
    shares a day on average (judged on that day, not today)

    python3 patterns.py              # writes patterns.json to stocks-feed
    python3 patterns.py --no-push
"""
import argparse
import datetime as dt
import json
import re
import sys

import numpy as np
import pandas as pd
import psycopg2

MIN_PRICE = 0.05
MIN_VOL = 20_000
FEE = 0.005
BANDS = [("Under C$0.50", 0.0, 0.5), ("C$0.50 to C$1", 0.5, 1.0), ("C$1 and up", 1.0, 1e9)]
HORIZONS = (1, 5, 20)
MIN_SORT = 100   # stocks needed on a date for the five-group sort

SECTORS = {
    "Gold": r"GOLD|\bAU\b|AURUM|\bORO\b",
    "Silver": r"SILVER|ARGENT|PLATA",
    "Copper": r"COPPER|\bCU\b",
    "Uranium": r"URANIUM|NUCLEAR|FISSION",
    "Lithium & battery metals": r"LITHIUM|GRAPHITE|NICKEL|COBALT|BATTER|VANADIUM|RARE EARTH",
    "Oil, gas & energy": r"ENERGY|\bOIL\b|PETRO|\bGAS\b|HYDROGEN|POWER",
    "Tech & AI": r"TECH|DIGITAL|\bDATA\b|\bAI\b|SOFTWARE|NETWORK|CYBER|QUANTUM|CLOUD|ROBOT|COMPUT|SYSTEMS",
    "Health & biotech": r"HEALTH|\bBIO|PHARMA|MEDIC|THERAP|CLINIC",
    "Cannabis": r"CANNA|HEMP",
}


def load(conn, years):
    since = dt.date.today() - dt.timedelta(days=int(years * 365.25) + 400)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('universe') IS NOT NULL")
        has_u = cur.fetchone()[0]
        src = ("SELECT ticker, name FROM universe UNION SELECT ticker, name FROM watchlist" if has_u
               else "SELECT ticker, name FROM watchlist")
        cur.execute(f"SELECT ticker, MAX(name) FROM ({src}) u GROUP BY ticker")
        names = dict(cur.fetchall())
        cur.execute("""SELECT d.ticker, d.trade_date, d.open, d.close, d.volume FROM daily_prices d
                       WHERE d.ticker = ANY(%s) AND d.trade_date >= %s""", (list(names), since))
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["t", "d", "o", "c", "v"])
    for k in ("o", "c", "v"):
        df[k] = pd.to_numeric(df[k], errors="coerce")
    df["d"] = pd.to_datetime(df["d"])
    df = df[df["c"] > 0]
    O = df.pivot(index="d", columns="t", values="o").sort_index()
    C = df.pivot(index="d", columns="t", values="c").reindex(O.index)
    V = df.pivot(index="d", columns="t", values="v").reindex(O.index)
    return O, C, V, names


def index_series(conn, years):
    import benchmarks
    import load_history
    with conn.cursor() as cur:
        cur.execute("SELECT MIN(trade_date) FROM daily_prices WHERE ticker = %s", (benchmarks.MAIN,))
        first = cur.fetchone()[0]
    if first is None or first > dt.date.today() - dt.timedelta(days=int(years * 365)):
        load_history.load(conn, benchmarks.MAIN, benchmarks.BENCHMARKS[benchmarks.MAIN], "max")
    s = benchmarks.history(conn, days=int(years * 365.25) + 400).get(benchmarks.MAIN, [])
    return pd.Series([c for _, c in s], index=pd.to_datetime([d for d, _ in s]))


def tstat(x):
    x = x.dropna()
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std() > 0 else 0.0


def pct(x):
    return None if x is None or pd.isna(x) else round(float(x) * 100, 2)


def consistency(per_year, sign):
    py = per_year.dropna()
    return int((np.sign(py) == sign).sum()), int(len(py))


def grade(years_same, years_all, avg_excess):
    if years_all < 4:
        return "Not enough years yet"
    share = years_same / years_all
    if share >= 0.75 and abs(avg_excess) >= 0.005:
        return "Consistent"
    if share >= 0.6:
        return "Leans this way"
    return "No reliable pattern"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect()
    try:
        O, C, V, names = load(conn, args.years)
        ix = index_series(conn, args.years)
    finally:
        conn.close()

    start = pd.Timestamp(dt.date.today() - dt.timedelta(days=int(args.years * 365.25)))
    ret = C.pct_change(fill_method=None)
    bad = ret.abs() > 3                      # data glitches: >300% in a day
    C = C.mask(bad)
    ret = ret.mask(bad)
    av20 = V.shift(1).rolling(20, min_periods=15).mean()
    elig = (C >= MIN_PRICE) & (av20 >= MIN_VOL)
    elig.loc[elig.index < start] = False
    entry = O.shift(-1).where(O.shift(-1) > 0, C.shift(-1))
    fwd = {k: (C.shift(-k) / entry - 1).where(lambda x: x.abs() < 5) for k in HORIZONS}
    cost = (np.where(entry < 0.5, 0.005, 0.01) / entry) + FEE
    years = sorted(set(C.index[C.index >= start].year))

    findings = []

    # ---------- big one-day moves, by price band ----------
    def event_table(mask, title, question, note):
        rows, verdicts = [], []
        for label, lo, hi in BANDS:
            band = elig & (C >= lo) & (C < hi)
            ev = mask & band
            n = int(ev.sum().sum())
            if n < 30:
                rows.append([label, n] + [None] * 5 + ["Too few"])
                continue
            cells = []
            for k in HORIZONS:
                ex = fwd[k].where(ev).mean(axis=1) - fwd[k].where(band).mean(axis=1)
                cells.append(ex)
            ex20 = cells[2]
            py = ex20.groupby(ex20.index.year).mean()
            avg20 = float(ex20.mean())
            same, tot = consistency(py, np.sign(avg20))
            net = float((fwd[20].where(ev) - pd.DataFrame(cost, index=C.index, columns=C.columns).where(ev)).stack().mean())
            g = grade(same, tot, avg20)
            verdicts.append((label, g, avg20, same, tot))
            rows.append([label, n, pct(cells[0].mean()), pct(cells[1].mean()), pct(avg20),
                         f"{same} of {tot}", pct(net), g])
        findings.append({
            "key": title.lower().replace(" ", "_")[:30], "title": title, "question": question,
            "columns": [{"label": "Price band"}, {"label": "Events", "fmt": "int"},
                        {"label": "Next day vs avg", "fmt": "pct"}, {"label": "5 days vs avg", "fmt": "pct"},
                        {"label": "20 days vs avg", "fmt": "pct"}, {"label": "Years same direction"},
                        {"label": "20-day result after costs", "fmt": "pct"}, {"label": "Verdict"}],
            "rows": rows, "note": note})

    up10, dn10 = ret >= 0.10, ret <= -0.10
    event_table(up10, "After a 10%+ up day", "Do big up days keep going, or fade?",
                "Bought at the next morning's open. 'vs avg' = compared with the average stock in the same price band that day.")
    event_table(dn10, "After a 10%+ down day", "Do big drops bounce back, or keep falling?",
                "Bought at the next morning's open, which removes most of the fake 'bounce' from closing prices.")
    vr = V / av20
    event_table((vr >= 3) & (ret > 0.03), "Volume spike on an up day",
                "Up 3%+ on 3x normal volume: is buying behind the move a good sign?", "Same method as above.")
    event_table((vr >= 3) & (ret < -0.03), "Volume spike on a down day",
                "Down 3%+ on 3x normal volume: is heavy selling a warning?", "Same method as above.")

    # ---------- cross-sectional sorts ----------
    sorts = [
        ("Past 1-month return", C / C.shift(21) - 1),
        ("Past 3-month return", C / C.shift(63) - 1),
        ("Past 6 months, skipping the last month", C.shift(21) / C.shift(126) - 1),
        ("Volatility (past 3 months)", ret.rolling(63, min_periods=40).std()),
        ("Closeness to 52-week high", C / C.rolling(252, min_periods=120).max()),
        ("Share price", C),
        ("Trading activity (average volume)", av20),
    ]
    srows = []
    for label, score in sorts:
        res, dates = [], []
        for i in range(0, len(C) - 21, 21):
            if C.index[i] < start:
                continue
            m = elig.iloc[i]
            s = score.iloc[i][m].dropna()
            r = fwd[20].iloc[i].reindex(s.index).dropna()
            s = s.reindex(r.index)
            if len(s) < MIN_SORT:
                continue
            q = pd.qcut(s.rank(method="first"), 5, labels=False)
            res.append([r[q == k].mean() for k in range(5)])
            dates.append(C.index[i])
        if not res:
            continue
        a = pd.DataFrame(res, index=dates)
        spread = a[4] - a[0]
        py = spread.groupby(spread.index.year).mean()
        same, tot = consistency(py, np.sign(spread.mean()))
        srows.append([label, pct(a[0].mean()), pct(a[2].mean()), pct(a[4].mean()), pct(spread.mean()),
                      f"{same} of {tot}", grade(same, tot, spread.mean())])
    findings.append({
        "key": "sorts", "title": "What predicts the next month?",
        "question": "Every month, stocks are split into five equal groups by each measure; this is how each group did over the following 20 trading days.",
        "columns": [{"label": "Measure"}, {"label": "Lowest fifth", "fmt": "pct"}, {"label": "Middle", "fmt": "pct"},
                    {"label": "Highest fifth", "fmt": "pct"}, {"label": "High minus low", "fmt": "pct"},
                    {"label": "Years same direction"}, {"label": "Verdict"}],
        "rows": srows,
        "note": "Average 20-day return per group, bought at the next open, before costs. Stocks that later stopped trading stay in."})

    # ---------- market conditions ----------
    ixr = ix.reindex(C.index).ffill()
    ma200 = ixr.rolling(200, min_periods=150).mean()
    up_mkt = (ixr > ma200)
    avg20 = fwd[20].where(elig).mean(axis=1)
    rrows = []
    for lab, m in (("Index above its 200-day average", up_mkt), ("Index below its 200-day average", ~up_mkt & ma200.notna())):
        x = avg20[m & (C.index >= start)].dropna()
        rrows.append([lab, int(len(x)), pct(x.mean()), round(float((x > 0).mean()) * 100) if len(x) else None])
    findings.append({
        "key": "regime", "title": "Does the market's trend matter?",
        "question": "Average 20-day return of a tradable Venture stock, depending on whether the Venture index was in an uptrend.",
        "columns": [{"label": "Market condition"}, {"label": "Days", "fmt": "int"},
                    {"label": "Avg stock, next 20 days", "fmt": "pct"}, {"label": "% of days positive"}],
        "rows": rrows, "note": "Bought at the next open, before costs."})

    # ---------- seasonality ----------
    ME = "ME" if pd.__version__ >= "2.2" else "M"
    me = C[C.index >= start].resample(ME).last()
    mret = (me / me.shift(1) - 1).where(lambda x: x.abs() < 5)
    elig_m = elig.resample(ME).last()
    med = mret.where(elig_m.shift(1, fill_value=False)).median(axis=1)
    ixm = ix[ix.index >= start].resample(ME).last().pct_change()
    mrows = []
    for mth in range(1, 13):
        x = med[med.index.month == mth].dropna()
        y = ixm[ixm.index.month == mth].dropna()
        if len(x) == 0:
            continue
        mrows.append([dt.date(2000, mth, 1).strftime("%B"), pct(x.mean()), f"{int((x > 0).sum())} of {len(x)}",
                      pct(y.mean()) if len(y) else None])
    findings.append({
        "key": "months", "title": "Best and worst months",
        "question": "Typical (median) Venture stock's return in each calendar month, averaged over the years.",
        "columns": [{"label": "Month"}, {"label": "Median stock", "fmt": "pct"}, {"label": "Years positive"},
                    {"label": "Venture index", "fmt": "pct"}],
        "rows": mrows, "note": "Few years per month, so only strong, repeated effects mean much (e.g. December tax-loss selling)."})

    dow = ret.where(elig).mean(axis=1)
    dow = dow[dow.index >= start]
    drows = [[d, pct(dow[dow.index.dayofweek == i].mean()), round(tstat(dow[dow.index.dayofweek == i] - dow.mean()), 1)]
             for i, d in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])]
    findings.append({
        "key": "weekdays", "title": "Day of the week",
        "question": "Average one-day move of a tradable stock, by weekday (close to close).",
        "columns": [{"label": "Day"}, {"label": "Avg stock", "fmt": "pct"}, {"label": "Strength (t)"}],
        "rows": drows, "note": "Strength above 3 or below -3 would be worth a look; smaller is noise."})

    # ---------- sectors ----------
    last = C.index[-1]
    recent = C.loc[:, C.iloc[-5:].notna().any()]  # still trading
    grp = {}
    for t in recent.columns:
        n = (names.get(t) or "").upper()
        grp[t] = next((k for k, p in SECTORS.items() if re.search(p, n)), None)
    grp = pd.Series(grp).dropna()

    def chg(days):
        return recent.iloc[-1] / recent.shift(days).iloc[-1] - 1
    c1, c3, c12 = chg(21), chg(63), chg(252)
    rr = ret[grp.index].iloc[-252:]
    sec_rows = []
    for g, ts in grp.groupby(grp):
        ts = list(ts.index)
        cm = rr[ts].corr().values
        tog = float(np.nanmean(cm[np.triu_indices(len(ts), 1)])) if len(ts) > 3 else None
        sec_rows.append([g, len(ts), pct(c1[ts].median()), pct(c3[ts].median()), pct(c12[ts].median()),
                         round(tog, 2) if tog is not None else None])
    sec_rows.sort(key=lambda r: -r[1])
    findings.append({
        "key": "sectors", "title": "Sectors right now",
        "question": "Median stock in each group (sorted from company names, so it's approximate).",
        "columns": [{"label": "Sector"}, {"label": "Stocks", "fmt": "int"}, {"label": "1 month", "fmt": "pct"},
                    {"label": "3 months", "fmt": "pct"}, {"label": "1 year", "fmt": "pct"},
                    {"label": "Move together (0-1)"}],
        "rows": sec_rows,
        "note": "'Move together' is how alike the stocks' daily moves are: near 0 = each on its own news, 0.4+ = they trade as a group."})

    in_period = C[C.index >= start]
    stopped = int((in_period.notna().any() & in_period.iloc[-20:].isna().all()).sum())
    out = {
        "generated": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
        "from": str(in_period.index[0].date()), "until": str(last.date()), "years": years,
        "stocks": int(in_period.notna().any().sum()), "stopped_trading": stopped,
        "rules": f"Tradable at the time: price C${MIN_PRICE:.2f}+ and {MIN_VOL:,}+ shares a day. Costs: one price step + {FEE:.1%}.",
        "findings": findings,
    }

    print(f"Patterns {out['from']} to {out['until']}: {out['stocks']} stocks ({stopped} no longer trading)")
    for f in findings:
        print(f"\n{f['title']}")
        for r in f["rows"]:
            print("   " + " | ".join("-" if v is None else str(v) for v in r))

    if args.no_push:
        return 0
    import export_feed
    feed = export_feed.FEED_DIR
    export_feed.git("pull", "--rebase", "--quiet")
    (feed / "patterns.json").write_text(json.dumps(out, separators=(",", ":"), default=str))
    export_feed.git("add", "patterns.json")
    if export_feed.git("diff", "--cached", "--quiet").returncode != 0:
        export_feed.git("commit", "-q", "-m", "patterns " + out["generated"])
        r = export_feed.git("push", "-q")
        if r.returncode != 0:
            print("Push failed:\n" + r.stderr, file=sys.stderr)
            return 1
    print("\npatterns.json pushed to stocks-feed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
