#!/usr/bin/env python3
"""
Publish the last 20 trading days of 5-minute bars for every tracked stock, so the
trading terminal can draw a 1-day chart for any of those days.

Files go to a separate branch ("bars") of the stocks-feed repo, one small
gzipped JSON per stock:  bars/LVG.V.json.gz  ->  {"ticker", "through", "days": {date: {...}}}
The branch keeps a single commit (amended and force-pushed each day), so the
repo doesn't grow.

    python3 export_bars.py
"""
import datetime as dt
import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg2

import intraday_archive

DAYS = 20
REPO = os.environ.get("BARS_REPO", "git@github.com:ipacitti88-sys/stocks-feed.git")
DIR = Path(os.environ.get("BARS_DIR", Path.home() / "stocks-feed-bars"))
SSH_KEY = Path.home() / ".ssh" / "stocks_feed"


def git(*args, check=False):
    env = dict(os.environ, GIT_SSH_COMMAND=f"ssh -i {SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new")
    r = subprocess.run(["git", "-C", str(DIR), *args], env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def ensure_repo():
    if (DIR / ".git").exists():
        return
    DIR.mkdir(parents=True, exist_ok=True)
    git("init", "-q", check=True)
    git("remote", "add", "origin", REPO, check=True)
    git("checkout", "-q", "--orphan", "bars", check=True)
    git("config", "user.name", "stocks server")
    git("config", "user.email", "stocks@localhost")


def main() -> int:
    conn = psycopg2.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('bars_5m') IS NOT NULL")
            if not cur.fetchone()[0]:
                print("No 5-minute archive yet.")
                return 0
            cur.execute("SELECT ticker FROM watchlist WHERE active")
            tickers = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT (ts AT TIME ZONE 'America/Toronto')::date AS d FROM bars_5m "
                        "WHERE ts > now() - interval '45 days' ORDER BY d DESC LIMIT %s", (DAYS,))
            days = sorted(r[0] for r in cur.fetchall())
        if not days:
            print("No recent 5-minute bars.")
            return 0
        per_day = intraday_archive.day_bars(conn, days[0], days[-1])
    finally:
        conn.close()

    ensure_repo()
    out = DIR / "bars"
    out.mkdir(exist_ok=True)
    for f in out.glob("*.json.gz"):
        f.unlink()
    n = 0
    for t in tickers:
        mine = {d: per_day[d][t] for d in sorted(per_day) if t in per_day[d]}
        if not mine:
            continue
        doc = {"ticker": t, "through": max(mine), "days": mine}
        with gzip.open(out / f"{t}.json.gz", "wt") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        n += 1
    # latest headlines per stock (written by news.py), for the terminal's stock panel
    nd, src = DIR / "news", Path(os.environ.get("WEB_DIR", Path(__file__).parent / "web")) / "news"
    nd.mkdir(exist_ok=True)
    for f in nd.glob("*.json"):
        f.unlink()
    for t in tickers:
        if (src / f"{t}.json").exists():
            (nd / f"{t}.json").write_text((src / f"{t}.json").read_text())
    (DIR / "README.md").write_text(f"5-minute bars, last {DAYS} trading days, per stock. Updated {dt.datetime.now():%Y-%m-%d %H:%M}.\n")

    git("add", "-A", check=True)
    has_commit = git("rev-parse", "--verify", "HEAD").returncode == 0
    msg = f"bars through {days[-1]}"
    git("commit", "-q", "--amend", "-m", msg) if has_commit else git("commit", "-q", "-m", msg)
    r = git("push", "-q", "-f", "origin", "bars")
    if r.returncode != 0:
        print("Push failed:\n" + r.stderr, file=sys.stderr)
        return 1
    print(f"1-day chart data for {n} stocks ({days[0]} to {days[-1]}) pushed to the 'bars' branch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
