"""Durable, idempotent operator holds for UNSTARTED queued tasks.

A hold pauses exactly one queued task. It never cancels the task, changes its ID,
recreates it, or reorders its queued siblings. Current status/reason live in
SQLite (`task_holds`), so a manager restart cannot lose a hold and dispatch can
never consume a held task.

Releasing a hold clears only this task-level hold: it never clears the global
pause flag, a per-provider/worker `dispatchEnabled` hold, a capacity/quota gate,
or a durable single-owner/recovery reservation. Those belong to their own
controls and remain in force across a release.
"""
import sqlite3
import time

from .store import emit

HELD = "held"
RELEASED = "released"
# A task that has left the queue and could already be in flight or be done.
_UNSTARTED = "queued"


def _reason(note):
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Hold/release reason must be a non-empty string")
    return note.strip()


def _require_unstarted(db, task_id, action):
    row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise ValueError("Task not found")
    if row[0] != _UNSTARTED:
        raise ValueError(
            f"Cannot {action} task {task_id}: state is {row[0]}; "
            "only an unstarted queued task can be held or released")
    return row[0]


def _row(db, task_id):
    try:
        return db.execute("SELECT reason,status,created,updated FROM task_holds "
                          "WHERE task_id=?", (task_id,)).fetchone()
    except sqlite3.OperationalError:
        # A legacy store without task_holds has no holds to report.
        return None


def is_held(db, task_id):
    row = _row(db, task_id)
    return bool(row) and row[1] == HELD


def held_ids(db):
    try:
        rows = db.execute("SELECT task_id FROM task_holds WHERE status=?", (HELD,)).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {row[0] for row in rows}


def current(db, task_id):
    """The current held status/reason for a task, or None when it is not held."""
    row = _row(db, task_id)
    if not row or row[1] != HELD:
        return None
    return {"taskId": task_id, "reason": row[0], "status": row[1],
            "created": row[2], "updated": row[3]}


def hold(db, task_id, note):
    """Hold an unstarted queued task. Idempotent; never cancels or recreates it."""
    reason = _reason(note)
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        _require_unstarted(db, task_id, "hold")
        row = _row(db, task_id)
        if row is not None and row[1] == HELD:
            db.rollback()
            return {"taskId": task_id, "held": True, "alreadyHeld": True, "reason": row[0]}
        now = time.time()
        if row is None:
            db.execute("INSERT INTO task_holds VALUES (?,?,?,?,?)",
                       (task_id, reason, HELD, now, now))
        else:
            db.execute("UPDATE task_holds SET reason=?,status=?,updated=? WHERE task_id=?",
                       (reason, HELD, now, task_id))
        emit(db, "task_held", taskId=task_id, reason=reason)
    except Exception:
        db.rollback()
        raise
    return {"taskId": task_id, "held": True, "alreadyHeld": False, "reason": reason}


def release(db, task_id, note):
    """Clear only this task-level hold. Idempotent; leaves every other gate in force."""
    reason = _reason(note)
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        _require_unstarted(db, task_id, "release")
        row = _row(db, task_id)
        if row is None or row[1] != HELD:
            db.rollback()
            return {"taskId": task_id, "held": False, "alreadyReleased": True}
        db.execute("UPDATE task_holds SET status=?,updated=? WHERE task_id=?",
                   (RELEASED, time.time(), task_id))
        emit(db, "task_released", taskId=task_id, reason=reason)
    except Exception:
        db.rollback()
        raise
    return {"taskId": task_id, "held": False, "alreadyReleased": False}
