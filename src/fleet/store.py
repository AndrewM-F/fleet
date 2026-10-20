"""The authoritative SQLite store: events (ack-tracked), worker snapshots, tasks."""
import json
import sqlite3
import time

from . import paths

TERMINAL = {"idle", "done", "completed", "failed", "error", "blocked", "stopped"}
BUSY = {"streaming", "queued", "working", "running", "active"}


_RECOVERIES_TABLE = """
CREATE TABLE IF NOT EXISTS recoveries (
  id TEXT PRIMARY KEY,
  failed_sid TEXT NOT NULL, failed_turn TEXT NOT NULL, failed_task_id TEXT NOT NULL,
  owner_sid TEXT NOT NULL, owner_task_id TEXT NOT NULL,
  ownership_task_id TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL,
  owned_worktree TEXT NOT NULL DEFAULT '',
  branch TEXT NOT NULL, head TEXT NOT NULL, worktree_sha256 TEXT NOT NULL,
  note TEXT NOT NULL, status TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL,
  UNIQUE(owner_task_id));
"""
_RECOVERIES_COLUMNS = ("id", "failed_sid", "failed_turn", "failed_task_id", "owner_sid",
                       "owner_task_id", "ownership_task_id", "cwd", "owned_worktree",
                       "branch", "head", "worktree_sha256", "note", "status",
                       "created", "updated")


def _retire_legacy_recoveries(db):
    """Drop the unconditional per-turn uniqueness so a retired authorization can be replaced.

    Older stores declared ``UNIQUE(failed_sid, failed_turn)``, which blocks authorizing a
    fresh successor for the same failed turn after the previous authorization was retired
    or reconciled. Replace it with an active-only unique index (created below). The failed
    task and turn rows themselves are copied verbatim and are never replayed.
    """
    # DDL, row copy, and old-table removal must be one SQLite transaction. In
    # particular, executescript() commits an open transaction before running;
    # using it between RENAME and INSERT can leave an empty new table shadowing
    # the legacy rows after a crash.
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='recoveries'"
        ).fetchone()
        if not row or not row[0] or "UNIQUE(failed_sid, failed_turn)" not in row[0]:
            db.commit()
            return
        present = [item[1] for item in db.execute("PRAGMA table_info(recoveries)")]
        copy = [name for name in _RECOVERIES_COLUMNS if name in present]
        db.execute("ALTER TABLE recoveries RENAME TO recoveries_legacy")
        db.execute(_RECOVERIES_TABLE)
        db.execute(f"INSERT INTO recoveries ({','.join(copy)}) "
                   f"SELECT {','.join(copy)} FROM recoveries_legacy")
        db.execute("DROP TABLE recoveries_legacy")
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()


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
    """)
    db.executescript(_RECOVERIES_TABLE)
    _retire_legacy_recoveries(db)
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
    CREATE UNIQUE INDEX IF NOT EXISTS recoveries_active_turn
      ON recoveries(failed_sid, failed_turn) WHERE status IN ('preparing','authorized','claimed');
    """)
    db.commit()
    return db


def emit(db, kind, **body):
    cur = db.execute("INSERT INTO events(kind,body,created) VALUES (?,?,?)",
                     (kind, json.dumps(body), time.time()))
    db.commit()
    return cur.lastrowid
