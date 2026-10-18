#!/usr/bin/env python3
"""
Phone notifications through ntfy (https://ntfy.sh), a free push service.

Set up once: install the ntfy app on your phone and subscribe to the topic named by
NTFY_TOPIC in ~/stocks/stocks.env (stocks-update creates a hard-to-guess one for you;
anyone who knows the name can read it, so keep it private). Leave NTFY_TOPIC empty to
turn notifications off. NTFY_URL can point to your own ntfy server.

    python3 notify.py --test        # send a test notification
    python3 notify.py --topic       # print the topic to subscribe to
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request

URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()


def enabled() -> bool:
    return bool(TOPIC)


def _hdr(v: str) -> str:
    # HTTP headers must be latin-1: keep titles plain
    return v.encode("latin-1", "replace").decode("latin-1")


def send(title: str, message: str, tags: str = "", priority: int = 3, click: str = "") -> bool:
    """Push one notification. Returns False (never raises) if it couldn't be sent."""
    if not TOPIC:
        return False
    req = urllib.request.Request(f"{URL}/{TOPIC}", data=message.encode("utf-8"), method="POST")
    req.add_header("Title", _hdr(title))
    req.add_header("Priority", str(priority))
    if tags:
        req.add_header("Tags", tags)
    if click:
        req.add_header("Click", click)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"notification not sent: {e}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--topic", action="store_true")
    a = ap.parse_args()
    if not TOPIC:
        print("Notifications are off: NTFY_TOPIC isn't set in ~/stocks/stocks.env.")
        return 1
    if a.topic or not a.test:
        print(f"Subscribe in the ntfy app to: {TOPIC}  (server {URL})")
    if a.test:
        ok = send("Stock alerts are working", "You'll get a notification here when a stock on your list hits "
                  "your buy or stop-loss level, or starts rising on heavy volume.", "white_check_mark")
        print("Test notification sent." if ok else "Couldn't send it; check the log above.")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
