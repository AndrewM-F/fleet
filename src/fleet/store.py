"""The authoritative SQLite store: events (ack-tracked), worker snapshots, tasks."""
import json
import sqlite3
import time

from . import paths

TERMINAL = {"idle", "done", "completed", "failed", "error", "blocked", "stopped"}
BUSY = {"streaming", "queued", "working", "running", "active"}


def database(path=None):
    db = sqlite3.connect(path or paths.DB, timeout=10)
    db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL,
      created REAL NOT NULL, acked INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS snapshots (
      sid TEXT PRIMARY KEY, body TEXT NOT NULL, changed REAL NOT NULL,
      observed REAL NOT NULL, stalled INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tasks (
      id TEXT PRIMARY KEY, sid TEXT NOT NULL, cwd TEXT NOT NULL,
      body TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL);
    """)
    db.commit()
    return db


def emit(db, kind, **body):
    cur = db.execute("INSERT INTO events(kind,body,created) VALUES (?,?,?)",
                     (kind, json.dumps(body), time.time()))
    db.commit()
    return cur.lastrowid
