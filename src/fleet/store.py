"""The authoritative SQLite store: events (ack-tracked), worker snapshots, tasks."""
import json
import sqlite3
import time

from . import paths

TERMINAL = {"idle", "done", "completed", "failed", "error", "blocked", "stopped"}
BUSY = {"streaming", "queued", "working", "running", "active"}


def database(path=None):
    if path is None:
        paths.DATA.mkdir(parents=True, exist_ok=True)
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
    CREATE TABLE IF NOT EXISTS task_holds (
      task_id TEXT PRIMARY KEY, reason TEXT NOT NULL, status TEXT NOT NULL,
      created REAL NOT NULL, updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS recoveries (
      id TEXT PRIMARY KEY,
      failed_sid TEXT NOT NULL, failed_turn TEXT NOT NULL, failed_task_id TEXT NOT NULL,
      owner_sid TEXT NOT NULL, owner_task_id TEXT NOT NULL,
      ownership_task_id TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL,
      owned_worktree TEXT NOT NULL DEFAULT '',
      branch TEXT NOT NULL, head TEXT NOT NULL, worktree_sha256 TEXT NOT NULL,
      note TEXT NOT NULL, status TEXT NOT NULL,
      created REAL NOT NULL, updated REAL NOT NULL,
      UNIQUE(failed_sid, failed_turn), UNIQUE(owner_task_id));
    """)
    recovery_columns = {row[1] for row in db.execute("PRAGMA table_info(recoveries)")}
    if "ownership_task_id" not in recovery_columns:
        db.execute("ALTER TABLE recoveries ADD COLUMN ownership_task_id TEXT NOT NULL DEFAULT ''")
    if "owned_worktree" not in recovery_columns:
        db.execute("ALTER TABLE recoveries ADD COLUMN owned_worktree TEXT NOT NULL DEFAULT ''")
    db.executescript("""
    CREATE UNIQUE INDEX IF NOT EXISTS recoveries_active_owner
      ON recoveries(owner_sid) WHERE status IN ('preparing','authorized','claimed');
    CREATE UNIQUE INDEX IF NOT EXISTS recoveries_active_cwd
      ON recoveries(cwd) WHERE status IN ('preparing','authorized','claimed');
    CREATE UNIQUE INDEX IF NOT EXISTS recoveries_active_worktree
      ON recoveries(owned_worktree)
      WHERE status IN ('preparing','authorized','claimed') AND owned_worktree<>'';
    """)
    db.commit()
    return db


def emit(db, kind, **body):
    cur = db.execute("INSERT INTO events(kind,body,created) VALUES (?,?,?)",
                     (kind, json.dumps(body), time.time()))
    db.commit()
    return cur.lastrowid
