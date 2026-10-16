#!/usr/bin/env python3
"""
Keep a copy of the trading terminal's own records on this server.

Some things only exist in the terminal's database on claude.ai: meme reviews,
pre-market notes, weekly picks, the signal track record, the paper account,
alerts and the terminal watchlist. A nightly Claude task exports them,
encrypts the file with this server's public key, and pushes it to the
"backup" branch of the stocks-feed repo. Only this server holds the private
key, so the public repo can't read it.

This script downloads that file, decrypts it and stores every record in the
terminal_backup table (records are only added or updated, never deleted, so
something removed in the terminal stays here). It also keeps a dated copy in
~/stocks/backups/ (last 120 days).

    python3 terminal_backup.py            # fetch and load the latest backup
    python3 terminal_backup.py --setup    # create the key pair (once) and publish the public key
    python3 terminal_backup.py --status   # what's stored
"""
import argparse
import datetime as dt
import gzip
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

import export_feed

HOME = Path.home()
KEY_DIR = HOME / ".config" / "stocks"
PRIV = KEY_DIR / "backup_key.pem"
PUB_NAME = "backup_pubkey.pem"
REPO_URL = "https://github.com/ipacitti88-sys/stocks-feed"
BACKUP_DIR = Path(os.environ.get("BACKUP_CLONE", HOME / "stocks-feed-backup"))
KEEP_DIR = Path(__file__).resolve().parent / "backups"
KEEP_DAYS = 120

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS terminal_backup (
    source     TEXT NOT NULL,            -- 'terminal' or 'weekly_picks'
    collection TEXT NOT NULL,
    doc_id     TEXT NOT NULL,
    data       JSONB NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, collection, doc_id)
);
"""

UPSERT_SQL = """
INSERT INTO terminal_backup (source, collection, doc_id, data) VALUES (%s, %s, %s, %s)
ON CONFLICT (source, collection, doc_id) DO UPDATE
  SET data = EXCLUDED.data, changed_at = now()
  WHERE terminal_backup.data IS DISTINCT FROM EXCLUDED.data
"""


def run(*cmd, **kw):
    return subprocess.run(list(cmd), capture_output=True, text=True, **kw)


def setup() -> int:
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)
    if not PRIV.exists():
        r = run("openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:4096", "-out", str(PRIV))
        if r.returncode != 0:
            print("Couldn't create the key: " + r.stderr, file=sys.stderr)
            return 1
        os.chmod(PRIV, 0o600)
        print(f"Created the backup key: {PRIV} (stays on this server)")
    pub = run("openssl", "pkey", "-in", str(PRIV), "-pubout")
    if pub.returncode != 0:
        print("Couldn't read the key: " + pub.stderr, file=sys.stderr)
        return 1
    feed = export_feed.FEED_DIR
    if not (feed / ".git").exists():
        print(f"No feed repo at {feed}.", file=sys.stderr)
        return 1
    export_feed.git("pull", "--rebase", "--quiet")
    target = feed / PUB_NAME
    if target.exists() and target.read_text() == pub.stdout:
        print("Public key already published.")
        return 0
    target.write_text(pub.stdout)
    export_feed.git("add", PUB_NAME)
    export_feed.git("commit", "-q", "-m", "backup public key")
    r = export_feed.git("push", "-q")
    if r.returncode != 0:
        print("Push failed:\n" + r.stderr, file=sys.stderr)
        return 1
    print("Public key published; the nightly terminal backup can now encrypt for this server.")
    return 0


def fetch() -> bool:
    """Bring ~/stocks-feed-backup up to date with the backup branch. False if there is no backup yet."""
    if not (BACKUP_DIR / ".git").exists():
        r = run("git", "clone", "-q", "--depth", "1", "--branch", "backup", REPO_URL, str(BACKUP_DIR))
        return r.returncode == 0
    r = run("git", "-C", str(BACKUP_DIR), "fetch", "-q", "--depth", "1", "origin", "backup")
    if r.returncode != 0:
        return False
    return run("git", "-C", str(BACKUP_DIR), "reset", "-q", "--hard", "FETCH_HEAD").returncode == 0


def decrypt() -> dict:
    enc, kenc = BACKUP_DIR / "backup.json.gz.enc", BACKUP_DIR / "backup.key.enc"
    if not enc.exists() or not kenc.exists():
        raise RuntimeError("the backup branch has no backup files yet")
    with tempfile.TemporaryDirectory() as td:
        k, out = Path(td) / "k.txt", Path(td) / "b.json.gz"
        r = run("openssl", "pkeyutl", "-decrypt", "-inkey", str(PRIV), "-pkeyopt", "rsa_padding_mode:oaep",
                "-pkeyopt", "rsa_oaep_md:sha256", "-in", str(kenc), "-out", str(k))
        if r.returncode != 0:
            raise RuntimeError("couldn't unlock the backup key: " + r.stderr.strip())
        r = run("openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000",
                "-in", str(enc), "-out", str(out), "-pass", f"file:{k}")
        if r.returncode != 0:
            raise RuntimeError("couldn't decrypt the backup: " + r.stderr.strip())
        raw = out.read_bytes()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass  # not gzipped
    return json.loads(raw)


def docs_of(backup: dict):
    """Yield (source, collection, doc_id, data). Accepts {"sources": {src: {coll: [{id, data}]}}}."""
    for src, colls in (backup.get("sources") or {}).items():
        for coll, docs in (colls or {}).items():
            for d in docs or []:
                if isinstance(d, dict) and d.get("id") is not None and isinstance(d.get("data"), dict):
                    yield src, coll, str(d["id"]), d["data"]


def load(backup: dict) -> tuple[int, int]:
    conn = psycopg2.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            cur.execute("SELECT COUNT(*) FROM terminal_backup")
            before = cur.fetchone()[0]
            n = 0
            for src, coll, doc_id, data in docs_of(backup):
                cur.execute(UPSERT_SQL, (src, coll, doc_id, Json(data)))
                n += 1
            cur.execute("SELECT COUNT(*) FROM terminal_backup")
            after = cur.fetchone()[0]
    finally:
        conn.close()
    return n, after - before


def keep_copy(backup: dict) -> Path:
    KEEP_DIR.mkdir(exist_ok=True)
    day = str(backup.get("exported") or dt.datetime.now().isoformat())[:10]
    path = KEEP_DIR / f"terminal-{day}.json.gz"
    with gzip.open(path, "wt") as fh:
        json.dump(backup, fh, separators=(",", ":"))
    cutoff = dt.date.today() - dt.timedelta(days=KEEP_DAYS)
    for f in KEEP_DIR.glob("terminal-????-??-??.json.gz"):
        try:
            if dt.date.fromisoformat(f.name[9:19]) < cutoff:
                f.unlink()
        except ValueError:
            pass
    return path


def status() -> int:
    conn = psycopg2.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('terminal_backup') IS NOT NULL")
            if not cur.fetchone()[0]:
                print("No terminal backup loaded yet.")
                return 0
            cur.execute("SELECT source, collection, COUNT(*), MAX(changed_at) FROM terminal_backup "
                        "GROUP BY 1, 2 ORDER BY 1, 2")
            for src, coll, n, last in cur.fetchall():
                print(f"{src:13} {coll:10} {n:5} records   last change {last:%Y-%m-%d %H:%M}")
    finally:
        conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.setup:
        return setup()
    if args.status:
        return status()
    if not PRIV.exists():
        print("No backup key yet; run: python3 terminal_backup.py --setup", file=sys.stderr)
        return 1
    if not fetch():
        print("No terminal backup published yet.")
        return 0
    try:
        backup = decrypt()
    except Exception as e:
        print(f"Backup not loaded: {e}", file=sys.stderr)
        return 1
    n, new = load(backup)
    path = keep_copy(backup)
    print(f"Terminal backup from {backup.get('exported', '?')}: {n} records checked, {new} new. Copy: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
