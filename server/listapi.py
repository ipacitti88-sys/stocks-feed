#!/usr/bin/env python3
"""
Tiny local service that lets the webpage add stocks to, and remove them from, "my list"
(the watchlist shared with the trading terminal). It listens on port 8081 on your home
network and Tailscale only; nothing on the internet can reach it.

    GET  /mylist                                  -> current list + changes waiting for the terminal
    POST /mylist {"ticker": "LVG.V", "action": "add" | "remove"}

Each change is saved to the list_changes table and published to the stocks-feed repo
straight away; the terminal applies it at its next sync (see mylist.py).

Started by cron (run_listapi.sh, every 5 minutes; a lock makes extra starts exit at once).
"""
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg2

import mylist

PORT = int(os.environ.get("LIST_API_PORT", "8081"))
HERE = Path(__file__).resolve().parent


def push_soon():
    """Publish waiting changes in the background, one push at a time."""
    py = HERE / "venv" / "bin" / "python"
    subprocess.Popen(
        ["flock", "/tmp/stocks-listpush.lock", str(py if py.exists() else sys.executable), str(HERE / "mylist.py"), "--push"],
        cwd=str(HERE), stdout=subprocess.DEVNULL, stderr=open(HERE / "logs" / "listapi.log", "a"),
        start_new_session=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "stocks-list/1"

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # lets a page on a public address (the Tailscale name) call this private one in Chrome
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def reply(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] != "/mylist":
            return self.reply(404, {"error": "not found"})
        try:
            conn = psycopg2.connect()
            try:
                return self.reply(200, mylist.state(conn))
            finally:
                conn.close()
        except Exception as e:
            return self.reply(500, {"error": f"database: {e.__class__.__name__}"})

    def do_POST(self):
        if self.path.split("?")[0] != "/mylist":
            return self.reply(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 2000:
            return self.reply(400, {"error": "bad request"})
        try:
            req = json.loads(self.rfile.read(n))
        except ValueError:
            return self.reply(400, {"error": "bad request"})
        try:
            conn = psycopg2.connect()
            try:
                ok, msg = mylist.change(conn, req.get("ticker"), req.get("action"))
                st = mylist.state(conn)
            finally:
                conn.close()
        except Exception as e:
            return self.reply(500, {"error": f"database: {e.__class__.__name__}"})
        if ok:
            push_soon()
        return self.reply(200 if ok else 400, {"ok": ok, "message": msg, **st})

    def log_message(self, fmt, *args):  # quiet: only errors go to the log
        pass


def main() -> int:
    (HERE / "logs").mkdir(exist_ok=True)
    conn = psycopg2.connect()
    try:
        mylist.ensure(conn)
    finally:
        conn.close()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"List service on port {PORT}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
