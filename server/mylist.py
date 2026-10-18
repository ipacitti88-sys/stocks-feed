#!/usr/bin/env python3
"""
"My list": the one watchlist shared by the trading terminal and this server's webpage.

The terminal on claude.ai keeps the master copy. Two files in the stocks-feed repo
carry changes between them:

  mylist branch, mylist.json   terminal -> server. Written by the terminal sync task
                               (hourly on weekdays, 6:40am to 1:40pm, plus 3:40pm): every stock on the
                               terminal's watchlist, plus "applied_through", the last
                               webpage change it has applied.
  main branch, list_changes.json
                               server -> terminal. Adds, removals and price-alert
                               levels set on the webpage that the terminal hasn't
                               applied yet.

The terminal also publishes its price alerts (buy at or below / stop-loss) in
mylist.json, so the webpage and the server's intraday alerts use the same levels.

Tables: my_list (the terminal's list as of its last sync) and list_changes (webpage
edits waiting for the terminal). The webpage shows my_list with the waiting edits
applied, so a change shows up there at once and in the terminal at its next sync.

    python3 mylist.py            # pull the terminal's list now
    python3 mylist.py --push     # publish waiting webpage changes now
    python3 mylist.py --status
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

import psycopg2

TICKER_RE = re.compile(r"^[A-Z0-9-]{1,10}\.(V|TO|CN|NE)$")
REPO_URL = "https://github.com/ipacitti88-sys/stocks-feed"
BRANCH = "mylist"
CLONE = Path.home() / "stocks-feed-mylist"
# always on the terminal's watchlist (they're built into the page), so they can't be removed
BUILT_IN = {"PRL.TO": "Propel Holdings", "XTRA.TO": "Xtract One Technologies", "PNG.V": "Kraken Robotics"}
SHORT = {t.split(".")[0]: t for t in BUILT_IN}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS my_list (
    ticker  TEXT PRIMARY KEY,
    name    TEXT,
    added   DATE,
    source  TEXT
);
CREATE TABLE IF NOT EXISTS list_changes (
    id           SERIAL PRIMARY KEY,
    ticker       TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('add', 'remove', 'alert')),
    name         TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE list_changes ADD COLUMN IF NOT EXISTS buy NUMERIC(14,4);
ALTER TABLE list_changes ADD COLUMN IF NOT EXISTS stop NUMERIC(14,4);
CREATE TABLE IF NOT EXISTS my_list_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def run(*cmd):
    return subprocess.run(list(cmd), capture_output=True, text=True)


def ensure(conn):
    with conn, conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        # tables made before price alerts only allowed add/remove: widen the check once
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'list_changes'::regclass AND conname = 'list_changes_action_check'")
        r = cur.fetchone()
        if r and "alert" not in r[0]:
            cur.execute("ALTER TABLE list_changes DROP CONSTRAINT list_changes_action_check")
            cur.execute("ALTER TABLE list_changes ADD CONSTRAINT list_changes_action_check "
                        "CHECK (action IN ('add', 'remove', 'alert'))")


def level(v):
    """A price level from the terminal or the webpage: positive float, or None for not set."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 4) if 0 < f < 100000 else None


def term_ticker(k: str) -> str:
    """The terminal keys its built-in stocks by short name (PRL); everything else is a Yahoo ticker."""
    k = str(k or "").strip().upper()
    return SHORT.get(k, k)


# ---------- terminal -> server ----------

def fetch_branch() -> Path | None:
    """Bring ~/stocks-feed-mylist up to date with the mylist branch. None if it doesn't exist yet."""
    if not (CLONE / ".git").exists():
        r = run("git", "clone", "-q", "--depth", "1", "--branch", BRANCH, REPO_URL, str(CLONE))
        if r.returncode != 0:
            return None
    else:
        r = run("git", "-C", str(CLONE), "fetch", "-q", "--depth", "1", "origin", BRANCH)
        if r.returncode != 0 or run("git", "-C", str(CLONE), "reset", "-q", "--hard", "FETCH_HEAD").returncode != 0:
            return None
    f = CLONE / "mylist.json"
    return f if f.exists() else None


def parse_alerts(raw: dict) -> dict:
    """{ticker: {"buy", "stop"}} from mylist.json's "alerts" (keys may be the terminal's short names)."""
    out = {}
    al = raw.get("alerts")
    if not isinstance(al, dict):
        return out
    for k, v in al.items():
        t = term_ticker(k)
        if not TICKER_RE.match(t) or not isinstance(v, dict):
            continue
        b, s = level(v.get("buy")), level(v.get("stop"))
        if b or s:
            out[t] = {"buy": b, "stop": s}
    return out


def parse(raw: dict) -> tuple[list[dict], int, str | None]:
    """Valid rows, applied_through and the terminal's timestamp from a mylist.json object."""
    rows, seen = [], set()
    for r in raw.get("tickers") or []:
        if not isinstance(r, dict):
            continue
        t = str(r.get("ticker") or "").strip().upper()
        if not TICKER_RE.match(t) or t in seen:
            continue
        seen.add(t)
        added = str(r.get("added") or "")[:10]
        try:
            dt.date.fromisoformat(added)
        except ValueError:
            added = None
        rows.append({"ticker": t, "name": str(r.get("name") or t)[:80], "added": added,
                     "source": str(r.get("from") or "terminal")[:30]})
    for t, n in BUILT_IN.items():
        if t not in seen:
            rows.append({"ticker": t, "name": n, "added": None, "source": "built-in"})
    try:
        through = int(raw.get("applied_through") or 0)
    except (TypeError, ValueError):
        through = 0
    return rows, through, raw.get("updated")


def apply(conn, rows: list[dict], through: int, updated: str | None, alerts: dict | None = None) -> dict:
    """Make my_list match the terminal and drop the webpage changes it has applied."""
    ensure(conn)
    keep = [r["ticker"] for r in rows]
    with conn, conn.cursor() as cur:
        cur.execute("SELECT ticker FROM my_list")
        before = {r[0] for r in cur.fetchall()}
        cur.execute("DELETE FROM my_list WHERE NOT (ticker = ANY(%s))", (keep,))
        for r in rows:
            cur.execute(
                "INSERT INTO my_list (ticker, name, added, source) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (ticker) DO UPDATE SET name = EXCLUDED.name, "
                "added = COALESCE(EXCLUDED.added, my_list.added), source = EXCLUDED.source",
                (r["ticker"], r["name"], r["added"] or dt.date.today().isoformat(), r["source"]))
        cur.execute("DELETE FROM list_changes WHERE id <= %s", (through,))
        cur.execute("INSERT INTO my_list_state (key, value) VALUES ('terminal_updated', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (updated or "",))
        if alerts is not None:
            cur.execute("INSERT INTO my_list_state (key, value) VALUES ('alerts', %s) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (json.dumps(alerts),))
    now = set(keep)
    return {"added": sorted(now - before), "removed": sorted(before - now), "count": len(now)}


def pull(conn) -> dict | None:
    """Fetch the terminal's list and apply it. None if the terminal hasn't published one yet."""
    f = fetch_branch()
    if not f:
        return None
    try:
        raw = json.loads(f.read_text())
    except (OSError, ValueError) as e:
        print(f"mylist.json unreadable: {e}", file=sys.stderr)
        return None
    raw = raw if isinstance(raw, dict) else {}
    rows, through, updated = parse(raw)
    # older terminal syncs didn't publish alerts: keep the last known levels then
    res = apply(conn, rows, through, updated, parse_alerts(raw) if "alerts" in raw else None)
    res["tickers"] = [r["ticker"] for r in rows]
    return res


# ---------- webpage -> terminal ----------

def state(conn) -> dict:
    """What the webpage shows: the terminal's list, waiting changes, and the list with them applied."""
    ensure(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT ticker, name, added, source FROM my_list ORDER BY ticker")
        base = [{"t": t, "n": n, "added": a.isoformat() if hasattr(a, "isoformat") else a, "from": s}
                for t, n, a, s in cur.fetchall()]
        cur.execute("SELECT id, ticker, action, name, requested_at, buy, stop FROM list_changes ORDER BY id")
        pend = [{"id": i, "t": t, "a": a, "n": n, "at": at.isoformat() if hasattr(at, "isoformat") else at,
                 **({"buy": level(b), "stop": level(s)} if a == "alert" else {})}
                for i, t, a, n, at, b, s in cur.fetchall()]
        cur.execute("SELECT value FROM my_list_state WHERE key = 'terminal_updated'")
        r = cur.fetchone()
        cur.execute("SELECT value FROM my_list_state WHERE key = 'alerts'")
        ra = cur.fetchone()
    try:
        alerts = json.loads(ra[0]) if ra and ra[0] else {}
    except ValueError:
        alerts = {}
    for p in pend:  # levels set here win until the terminal confirms them
        if p["a"] == "alert":
            if p["buy"] or p["stop"]:
                alerts[p["t"]] = {"buy": p["buy"], "stop": p["stop"], "pending": True}
            else:
                alerts[p["t"]] = {"buy": None, "stop": None, "pending": True}
    on = {b["t"]: b for b in base}
    for p in pend:
        if p["a"] == "alert":
            continue
        if p["a"] == "add":
            on.setdefault(p["t"], {"t": p["t"], "n": p["n"] or p["t"], "added": str(p["at"])[:10],
                                   "from": "webpage"})["pending"] = "add"
        elif p["t"] in on:
            on[p["t"]]["pending"] = "remove"
    return {"items": sorted(on.values(), key=lambda x: x["t"]), "pending": pend, "alerts": alerts,
            "builtIn": sorted(BUILT_IN), "terminalUpdated": (r[0] if r else None) or None}


def alert_levels(conn) -> dict:
    """{ticker: {"buy", "stop"}}: the levels in force (terminal's, with webpage edits applied)."""
    return {t: a for t, a in state(conn)["alerts"].items() if a.get("buy") or a.get("stop")}


def set_alert(conn, t: str, buy, stop) -> tuple[bool, str]:
    """Record buy / stop-loss levels set on the webpage (0 or empty clears one)."""
    raw = {"buy": buy, "stop": stop}
    for k, v in raw.items():
        if v not in (None, "", 0, "0") and level(v) is None:
            return False, f"The {'buy' if k == 'buy' else 'stop-loss'} level must be a price above 0."
    b, s = level(buy), level(stop)
    ensure(conn)
    with conn, conn.cursor() as cur:
        # replace an earlier edit the terminal hasn't applied yet
        cur.execute("DELETE FROM list_changes WHERE ticker = %s AND action = 'alert'", (t,))
        cur.execute("SELECT name FROM watchlist WHERE ticker = %s", (t,))
        r = cur.fetchone()
        cur.execute("INSERT INTO list_changes (ticker, action, name, buy, stop) VALUES (%s, 'alert', %s, %s, %s)",
                    (t, (r[0] if r else None) or t, b, s))
    if not b and not s:
        return True, f"Cleared the alerts on {t}. The terminal picks it up at its next sync."
    bits = ([f"buy at or below C${b:g}"] if b else []) + ([f"stop-loss C${s:g}"] if s else [])
    return True, f"{t}: {' and '.join(bits)}. Saved; the terminal picks it up at its next sync."


def change(conn, ticker: str, action: str, name: str | None = None, buy=None, stop=None) -> tuple[bool, str]:
    """Record a webpage add, remove or alert change. Returns (ok, message)."""
    t = str(ticker or "").strip().upper()
    if not TICKER_RE.match(t):
        return False, "Not a valid ticker."
    if action not in ("add", "remove", "alert"):
        return False, "Unknown action."
    if action == "alert":
        return set_alert(conn, t, buy, stop)
    if action == "remove" and t in BUILT_IN:
        return False, f"{t} is built into the terminal and can't be removed."
    ensure(conn)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM watchlist WHERE ticker = %s AND active", (t,))
        r = cur.fetchone()
        # a stock the server doesn't track yet (e.g. a weekly pick) is fine: sync_requests.py
        # starts tracking it within 30 minutes on weekdays
        name = (r[0] if r else None) or str(name or "").strip()[:80] or t
        cur.execute("SELECT 1 FROM my_list WHERE ticker = %s", (t,))
        on_terminal = cur.fetchone() is not None
        cur.execute("SELECT id, action FROM list_changes WHERE ticker = %s AND action <> 'alert' ORDER BY id DESC LIMIT 1", (t,))
        last = cur.fetchone()
        on_now = (last[1] == "add") if last else on_terminal
        if (action == "add") == on_now:
            return True, f"{t} is already {'on' if on_now else 'off'} your list."
        if last:
            # undoing a change the terminal hasn't seen yet: just forget it
            cur.execute("DELETE FROM list_changes WHERE id = %s", (last[0],))
            return True, f"{'Kept' if action == 'add' else 'Took back'} {t}; the terminal won't see any change."
        cur.execute("INSERT INTO list_changes (ticker, action, name) VALUES (%s, %s, %s)", (t, action, name))
    return True, f"{'Added' if action == 'add' else 'Removed'} {t}. The terminal picks it up at its next sync." + (
        "" if r or action == "remove" else " Your server starts tracking it within 30 minutes on weekdays.")


def write_changes(conn, feed_dir: Path) -> Path:
    """Write list_changes.json into the feed folder (the caller commits it)."""
    ensure(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id, ticker, action, name, requested_at, buy, stop FROM list_changes ORDER BY id")
        rows = cur.fetchall()
    out = {"generated": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
           "changes": [{"id": i, "ticker": t, "action": a, "name": n or t,
                        "at": at.isoformat(timespec="minutes") if hasattr(at, "isoformat") else str(at),
                        # alert: the new levels, 0 = not set
                        **({"buy": level(b) or 0, "stop": level(s) or 0} if a == "alert" else {})}
                       for i, t, a, n, at, b, s in rows]}
    p = feed_dir / "list_changes.json"
    p.write_text(json.dumps(out, indent=1))
    return p


def push_changes() -> int:
    """Publish list_changes.json to the repo right away (run after each webpage edit)."""
    import export_feed
    feed = export_feed.FEED_DIR
    if not (feed / ".git").exists():
        return 0
    export_feed.git("pull", "--rebase", "--quiet")
    conn = psycopg2.connect()
    try:
        write_changes(conn, feed)
    finally:
        conn.close()
    export_feed.git("add", "list_changes.json")
    if export_feed.git("diff", "--cached", "--quiet").returncode == 0:
        return 0
    export_feed.git("commit", "-q", "-m", "my list / alert changes from the webpage")
    for _ in range(2):
        if export_feed.git("push", "-q").returncode == 0:
            return 0
        export_feed.git("pull", "--rebase", "--quiet")
    print("Couldn't push list_changes.json", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.push:
        return push_changes()
    conn = psycopg2.connect()
    try:
        if a.status:
            s = state(conn)
            print(f"{len(s['items'])} stocks on your list; {len(s['pending'])} webpage change(s) waiting for the terminal.")
            for i in s["items"]:
                print(f"  {i['t']:10} {i.get('pending', ''):7} {i['n']}")
            al = {t: a for t, a in s["alerts"].items() if a.get("buy") or a.get("stop")}
            if al:
                print("Price alerts:")
                for t, a in sorted(al.items()):
                    print(f"  {t:10} buy <= {a.get('buy') or '-':<8} stop {a.get('stop') or '-':<8}"
                          f"{' (waiting for the terminal)' if a.get('pending') else ''}")
            return 0
        res = pull(conn)
        print("The terminal hasn't published its list yet." if res is None else
              f"My list: {res['count']} stocks (+{len(res['added'])} / -{len(res['removed'])}).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
