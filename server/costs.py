#!/usr/bin/env python3
"""
What a stock really costs to trade, and how much of it you can buy and still get out.

Yahoo gives prices, not the bid and ask, so the spread is estimated from daily highs,
lows and closes with the Abdi-Ranaldo method (Review of Financial Studies, 2017), over
the last 60 trading days, and never below one price step (C$0.005 under C$0.50,
C$0.01 above; at C$0.10 one step is already 5%).

For each tracked stock:
  spread   estimated bid-ask spread, % of the price (what a buy-then-sell loses to it)
  rt       estimated round trip: spread + 0.5% for fees and slippage (the lab's allowance)
  mdv      median dollars traded a day, last 20 days (the median ignores one-day spikes)
  maxpos   a position you could sell in about a day: 10% of mdv
  quiet    share of the last 20 days' 5-minute bars with no trades
  atr      typical daily move: 14-day average true range, % of the price
  grade    low (round trip under 2%), medium (2-5%) or high (over 5%)

    python3 costs.py              # the costliest and cheapest stocks to trade
    python3 costs.py LVG.V PNG.V
"""
from __future__ import annotations

import datetime as dt
import sys

import numpy as np
import pandas as pd
import psycopg2

FEE = 0.005             # same allowance as the signal lab
WINDOW = 60             # trading days for the spread estimate
MIN_OBS = 20
VOL_SHARE = 0.10        # trade at most ~10% of a day's dollar volume


def tick(p):
    return np.where(np.asarray(p, float) < 0.5, 0.005, 0.01)


def add_spread(df: pd.DataFrame, shift: int = 1) -> pd.DataFrame:
    """Add "spr": the estimated spread (fraction of price) known at each row's close, per ticker.
    df needs ticker, high, low, close in date order per ticker. shift=1 keeps it free of
    look-ahead (each two-day term needs the next day's high and low)."""
    g = df.groupby("ticker", sort=False)
    c = np.log(df["close"].where(df["close"] > 0))
    eta = (np.log(df["high"].where(df["high"] > 0)) + np.log(df["low"].where(df["low"] > 0))) / 2
    eta_next = eta.groupby(df["ticker"], sort=False).shift(-1)
    term = (4 * (c - eta) * (c - eta_next)).clip(lower=0)
    # the term for day t uses day t+1, so at day t's close only terms up to t-1 are known
    term = term.groupby(df["ticker"], sort=False).shift(shift)
    s2 = term.groupby(df["ticker"], sort=False).transform(lambda x: x.rolling(WINDOW, min_periods=MIN_OBS).mean())
    df["spr"] = np.sqrt(s2)
    floor = tick(df["close"]) / df["close"]
    df["spr"] = np.maximum(df["spr"].fillna(floor), floor)
    return df


def add_atr(df: pd.DataFrame) -> pd.DataFrame:
    """Add "atrp": 14-day average true range as a fraction of the close, per ticker."""
    g = df.groupby("ticker", sort=False)
    pc = g["close"].shift(1)
    tr = np.maximum(df["high"], pc.fillna(df["high"])) - np.minimum(df["low"], pc.fillna(df["low"]))
    atr = tr.groupby(df["ticker"], sort=False).transform(lambda x: x.rolling(14, min_periods=10).mean())
    df["atrp"] = atr / df["close"]
    return df


def grade(rt: float) -> str:
    return "low" if rt < 0.02 else "medium" if rt <= 0.05 else "high"


def current(conn, tickers: list[str] | None = None) -> dict:
    """{ticker: {spread, rt, mdv, maxpos, quiet, atr, grade, days}} for tracked stocks (fractions, C$)."""
    since = dt.date.today() - dt.timedelta(days=130)
    with conn.cursor() as cur:
        cur.execute("""SELECT d.ticker, d.trade_date, d.high, d.low, d.close, d.volume FROM daily_prices d
                       JOIN watchlist w ON w.ticker = d.ticker AND w.active
                       WHERE d.trade_date >= %s ORDER BY d.ticker, d.trade_date""", (since,))
        rows = cur.fetchall()
        quiet = {}
        cur.execute("SELECT to_regclass('bars_5m') IS NOT NULL")
        if cur.fetchone()[0]:
            # in-session bars only (9:30-16:00 Eastern), last 20 trading days
            cur.execute("""
                WITH b AS (
                  SELECT ticker, ts AT TIME ZONE 'America/Toronto' AS lt, volume FROM bars_5m
                  WHERE ts >= now() - interval '35 days')
                SELECT ticker, AVG(CASE WHEN COALESCE(volume, 0) = 0 THEN 1.0 ELSE 0.0 END), COUNT(*)
                FROM b WHERE lt::time >= '09:30' AND lt::time < '16:00'
                  AND lt::date >= (SELECT MIN(d) FROM (SELECT DISTINCT lt::date AS d FROM b ORDER BY 1 DESC LIMIT 20) x)
                GROUP BY ticker""")
            quiet = {t: (float(q), n) for t, q, n in cur.fetchall()}
    if not rows:
        return {}
    df = pd.DataFrame(rows, columns=["ticker", "date", "high", "low", "close", "volume"])
    for c in ("high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    if tickers:
        df = df[df["ticker"].isin(tickers)].reset_index(drop=True)
    df = add_spread(df, shift=0)   # today: use every term we have
    df = add_atr(df)
    df["dv"] = df["close"] * df["volume"].fillna(0)
    out = {}
    for t, g in df.groupby("ticker", sort=False):
        last = g.iloc[-1]
        n = int(g["close"].notna().sum())
        spr = float(last["spr"])
        mdv = float(g["dv"].tail(20).median())
        q = quiet.get(t)
        rt = spr + FEE
        out[t] = {"px": round(float(last["close"]), 4), "spread": round(spr, 4), "rt": round(rt, 4), "mdv": round(mdv), "maxpos": int(round(mdv * VOL_SHARE, -2)),
                  "quiet": round(q[0], 2) if q and q[1] >= 200 else None,
                  "atr": round(float(last["atrp"]), 4) if pd.notna(last["atrp"]) else None,
                  "grade": grade(rt), "days": min(n, WINDOW)}
    return out


def main() -> int:
    conn = psycopg2.connect()
    try:
        c = current(conn, [t.upper() for t in sys.argv[1:]] or None)
    finally:
        conn.close()
    if not c:
        print("No price history yet.")
        return 0
    rows = sorted(c.items(), key=lambda kv: -kv[1]["rt"])
    show = rows if len(sys.argv) > 1 else rows[:15] + [("...", None)] + rows[-5:]
    print(f"{'stock':10} {'round trip':>10} {'spread':>7} {'C$/day':>9} {'max pos':>8} {'quiet':>6} {'ATR':>6}")
    for t, v in show:
        if v is None:
            print("  ...")
            continue
        print(f"{t:10} {v['rt']:>9.1%} {v['spread']:>7.1%} {v['mdv']:>9,} {v['maxpos']:>8,} "
              f"{'' if v['quiet'] is None else format(v['quiet'], '.0%'):>6} {'' if v['atr'] is None else format(v['atr'], '.1%'):>6}")
    by = {}
    for v in c.values():
        by[v["grade"]] = by.get(v["grade"], 0) + 1
    print(f"{len(c)} stocks: {by.get('low', 0)} low cost, {by.get('medium', 0)} medium, {by.get('high', 0)} high (round trip over 5%).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
