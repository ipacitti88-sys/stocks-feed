#!/usr/bin/env python3
"""
Push a small summary of the dashboard to the private GitHub repo
ipacitti88-sys/stocks-feed, so Claude's scheduled checks can read it.

    python3 export_feed.py

Writes latest.md (for reading), latest.json (for tools) and a dated copy in
history/, then commits and pushes. Only summary rows leave the server —
never credentials or the full database.
"""
import datetime as dt
import decimal
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg2

import dashboard  # reuses the screen query

FEED_DIR = Path(os.environ.get("FEED_DIR", Path.home() / "stocks-feed"))
SSH_KEY = Path.home() / ".ssh" / "stocks_feed"
MIN_LIQUIDITY = 50_000
TOP_N = 10


def num(v):
    return float(v) if isinstance(v, decimal.Decimal) else v


def collect():
    conn = psycopg2.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(dashboard.SCREEN_SQL)
            screen = cur.fetchall()
            cur.execute("SELECT COUNT(*), MAX(trade_date) FROM daily_prices")
            _, last_day = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM watchlist WHERE active")
            tracked = cur.fetchone()[0]
            live = []
            cur.execute("SELECT to_regclass('intraday_quotes') IS NOT NULL")
            if cur.fetchone()[0]:
                cur.execute("""
                    SELECT q.ticker, COALESCE(w.name, q.ticker), q.as_of, q.last, q.pct_day,
                           q.pct_30m, q.pace_rel_vol, q.rising, q.first_rising_at
                    FROM intraday_quotes q LEFT JOIN watchlist w ON w.ticker = q.ticker
                    WHERE q.trade_date = (SELECT MAX(trade_date) FROM intraday_quotes)""")
                live = cur.fetchall()
        breakouts = []
        try:  # Breakout from a quiet base, C$0.50+: the signal that held up over 10 years
            import signal_lab as sl
            df = sl.add_indicators(sl.load_daily(conn, dt.date.today() - dt.timedelta(days=200)))
            m = sl.signal_masks(df)["breakout"]
            day = df["date"].max()
            hit = df[m & df["tracked"] & (df["date"] == day) & df["close"].between(0.50, sl.MAX_PRICE)
                     & (df["av20"] >= sl.MIN_LIQ) & (df["rn"] >= sl.MIN_HISTORY)]
            breakouts = [dict(ticker=t, name=n, date=str(d), close=round(float(c), 4),
                              rel_vol=round(float(v / a), 1) if a else None)
                         for t, n, d, c, v, a in hit[["ticker", "name", "date", "close", "volume", "av20"]]
                         .itertuples(index=False)]
            breakouts.sort(key=lambda x: -(x["rel_vol"] or 0))
        except Exception as e:
            print(f"breakout scan failed: {e}", file=sys.stderr)
        try:
            import benchmarks
            bench = benchmarks.summary(conn)
        except Exception as e:
            print(f"benchmark summary failed: {e}", file=sys.stderr)
            bench = {}
    finally:
        conn.close()

    stocks = []
    for (t, d, close, rv, p20, fh, avgv, ma, spike, nh, name, added) in screen:
        stocks.append(dict(ticker=t, name=name, date=str(d), close=num(close), rel_vol=num(rv),
                           pct_20d=num(p20), from_52w_high=num(fh), avg_vol=num(avgv),
                           signals=sum(bool(x) for x in (ma, spike, nh)),
                           above_ma50=bool(ma), vol_spike=bool(spike), new_high=bool(nh),
                           added=str(added) if added else None))
    flagged = sorted([s for s in stocks if s["signals"] >= 2 and (s["avg_vol"] or 0) >= MIN_LIQUIDITY],
                     key=lambda s: (-s["signals"], -(s["rel_vol"] or 0)))
    by_relvol = sorted([s for s in stocks if (s["avg_vol"] or 0) >= MIN_LIQUIDITY],
                       key=lambda s: -(s["rel_vol"] or 0))[:TOP_N]

    live_rows = [dict(ticker=t, name=n, as_of=as_of.isoformat() if as_of else None, last=num(last),
                      pct_day=num(pd), pct_30m=num(p30), volume_pace=num(pace), rising=bool(r),
                      rising_since=since.isoformat() if since else None)
                 for t, n, as_of, last, pd, p30, pace, r, since in live]
    rising = sorted([r for r in live_rows if r["rising"]], key=lambda r: -(r["pct_day"] or 0))
    gainers = sorted([r for r in live_rows if r["pct_day"] is not None], key=lambda r: -r["pct_day"])[:TOP_N]
    live_as_of = max((r["as_of"] for r in live_rows if r["as_of"]), default=None)

    return dict(generated=dt.datetime.now().astimezone().isoformat(timespec="minutes"),
                last_close_day=str(last_day) if last_day else None, tracked=tracked,
                flagged=flagged, top_rel_volume=by_relvol,
                live_as_of=live_as_of, rising=rising, top_gainers_today=gainers,
                benchmarks=bench, breakouts=breakouts)


PRICES_SQL = """
SELECT x.ticker, COALESCE(w.name, x.ticker), x.trade_date, x.close, x.volume FROM (
  SELECT d.ticker, d.trade_date, d.close, d.volume,
         ROW_NUMBER() OVER (PARTITION BY d.ticker ORDER BY d.trade_date DESC) AS rn
  FROM daily_prices d JOIN watchlist w ON w.ticker = d.ticker AND w.active
) x JOIN watchlist w ON w.ticker = x.ticker
WHERE x.rn <= 253
ORDER BY x.ticker, x.trade_date
"""


def collect_prices():
    """Last ~year of closes for every tracked stock, for the trading terminal page."""
    conn = psycopg2.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(PRICES_SQL)
            rows = cur.fetchall()
    finally:
        conn.close()
    out = {}
    for t, name, d, close, vol in rows:
        e = out.setdefault(t, {"name": name, "dates": [], "closes": [], "vols": []})
        e["dates"].append(str(d))
        e["closes"].append(round(float(close), 4) if close is not None else None)
        e["vols"].append(int(vol) if vol is not None else 0)
    prices = {}
    for t, e in out.items():
        c = [v for v in e["closes"] if v is not None]
        if not c:
            continue
        v20 = e["vols"][-20:]
        prices[t] = {"name": e["name"], "date": e["dates"][-1], "last": c[-1],
                     "prev": c[-2] if len(c) > 1 else c[-1],
                     "hi": max(c), "lo": min(c),
                     "avgVol": round(sum(v20) / len(v20)) if v20 else 0,
                     "closes": c}
    return prices


def fmt(v, suf=""):
    return "-" if v is None else f"{v:+.1f}{suf}" if suf == "%" else f"{v}{suf}"


def to_markdown(f):
    L = [f"# Stock feed — {f['generated']}", "",
         f"Tracking {f['tracked']} stocks. Last full trading day: {f['last_close_day']}. "
         f"Intraday data as of: {f['live_as_of'] or 'none'} (Yahoo, ~15 min delayed).", ""]
    for b in (f.get("benchmarks") or {}).values():
        lv = b.get("live")
        L += [f"Market: {b['name']} {b['last']:,.2f} on {b['date']} (day {fmt(b['chg_1d'], '%')}, "
              f"month {fmt(b['chg_1m'], '%')}, year {fmt(b['chg_1y'], '%')})"
              + (f"; live {fmt(lv['pct_day'], '%')} at {lv['as_of'][11:16]}" if lv else "")]
    if f.get("benchmarks"):
        L += [""]
    L += ["## Breakouts at last close (C$0.50+: new 60-day high after a quiet 20-day range, volume 2x+)", ""]
    L += [f"- {b['ticker']} ({b['name']}): close {b['close']}, volume {fmt(b['rel_vol'], 'x')}"
          for b in f.get("breakouts") or []] or ["- none"]
    L += [""]
    L += ["## Flagged at last close (2+ of: above 50-day avg, volume spike >2x, new 52-week high)", ""]
    L += [f"- {s['ticker']} ({s['name']}): {s['signals']}/3, close {s['close']}, rel vol {fmt(s['rel_vol'], 'x')}, "
          f"20d {fmt(s['pct_20d'], '%')}, {fmt(s['from_52w_high'], '%')} vs 52w high" for s in f["flagged"]] or ["- none"]
    L += ["", "## Rising (latest intraday pass: up 3%+, still climbing, volume pace 1.5x+)", ""]
    L += [f"- {r['ticker']} ({r['name']}): {fmt(r['pct_day'], '%')} today at {r['last']}, last 30m {fmt(r['pct_30m'], '%')}, "
          f"volume pace {fmt(r['volume_pace'], 'x')}, rising since {r['rising_since']}" for r in f["rising"]] or ["- none"]
    L += ["", f"## Top {TOP_N} by relative volume at last close", ""]
    L += [f"- {s['ticker']} ({s['name']}): rel vol {fmt(s['rel_vol'], 'x')}, close {s['close']}, 20d {fmt(s['pct_20d'], '%')}"
          for s in f["top_rel_volume"]] or ["- none"]
    if f["top_gainers_today"]:
        L += ["", f"## Top {TOP_N} gainers in latest intraday pass", ""]
        L += [f"- {r['ticker']} ({r['name']}): {fmt(r['pct_day'], '%')} at {r['last']}, volume pace {fmt(r['volume_pace'], 'x')}"
              for r in f["top_gainers_today"]]
    return "\n".join(L) + "\n"


def git(*args):
    env = dict(os.environ, GIT_SSH_COMMAND=f"ssh -i {SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new")
    return subprocess.run(["git", "-C", str(FEED_DIR), *args], env=env, capture_output=True, text=True)


def main() -> int:
    if not (FEED_DIR / ".git").exists():
        print(f"No feed repo at {FEED_DIR}; run feed_setup.sh first.", file=sys.stderr)
        return 1
    git("pull", "--rebase", "--quiet")
    feed = collect()
    (FEED_DIR / "latest.json").write_text(json.dumps(feed, indent=1, default=str))
    md = to_markdown(feed)
    (FEED_DIR / "latest.md").write_text(md)
    (FEED_DIR / "prices.json").write_text(json.dumps(collect_prices(), separators=(",", ":")))
    hist = FEED_DIR / "history"
    hist.mkdir(exist_ok=True)
    (hist / f"{dt.date.today().isoformat()}.md").write_text(md)

    git("add", "-A")
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("Feed unchanged.")
        return 0
    git("commit", "-q", "-m", f"feed {feed['generated']}")
    r = git("push", "-q")
    if r.returncode != 0:
        print("Push failed:\n" + r.stderr, file=sys.stderr)
        return 1
    print(f"Feed pushed: {len(feed['flagged'])} flagged, {len(feed['rising'])} rising.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
