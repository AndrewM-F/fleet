"""Dispatch queued tasks at most once to idle, non-conflicting workers."""
import json
from pathlib import Path
import time

from . import claude, pi
from .store import BUSY, emit


def dispatch(db, slots, states, sender, paused=False, max_concurrent=0):
    if paused:
        return
    active = db.execute("SELECT count(*) FROM tasks WHERE state IN ('dispatching','sent','running')").fetchone()[0]
    for task_id, sid, cwd, raw in db.execute(
            "SELECT id,sid,cwd,body FROM tasks WHERE state='queued' ORDER BY updated").fetchall():
        cwd = str(Path(cwd).resolve())
        # Unknown ownership blocks all work; a known workspace only blocks itself.
        if any(s["state"] in {"unavailable", "unknown"} and
               (not s.get("cwd") or str(Path(s["cwd"]).resolve()) == cwd)
               for s in states.values()):
            continue
        slot = slots[sid]
        state = states.get(sid, {})
        if state.get("state") not in {"idle", "done", "completed", "stopped"}:
            continue
        if slot.get("provider") in {"claude", "opencode"} and not slot.get("dispatchEnabled", False):
            continue
        if any(other != sid and s["state"] in (BUSY | {"blocked"}) and
               s.get("cwd") and str(Path(s["cwd"]).resolve()) == cwd
               for other, s in states.items()):
            continue
        if db.execute("SELECT 1 FROM tasks WHERE cwd=? AND state IN ('dispatching','sent','running','uncertain')", (cwd,)).fetchone():
            continue
        if max_concurrent and active >= max_concurrent:
            return
        task = json.loads(raw)
        task["baselineProgress"] = state.get("progress")
        claimed = db.execute(
            "UPDATE tasks SET state='dispatching',body=?,updated=? WHERE id=? AND state='queued'",
            (json.dumps(task), time.time(), task_id))
        db.commit()  # Crash after this point means uncertain delivery; never resend.
        if claimed.rowcount != 1:
            continue  # An operator cancelled this task after the queue snapshot.
        try:
            if slot.get("provider") == "opencode":
                sender(slot, task["message"], task_id=task_id)
            else:
                sender(slot, task["message"])
        except Exception as error:
            db.execute("UPDATE tasks SET state='uncertain' WHERE id=?", (task_id,))
            emit(db, "task_uncertain", taskId=task_id, sessionId=sid, error=str(error))
        else:
            db.execute("UPDATE tasks SET state='sent',updated=? WHERE id=?", (time.time(), task_id))
            emit(db, "task_sent", taskId=task_id, sessionId=sid)
        db.commit()
        active += 1
        states[sid] = dict(state, state="queued")


def send(slot, message, task_id=None):
    if slot.get("provider") == "opencode":
        if slot.get("mode") not in (None, "opencode"):
            raise ValueError("OpenCode workers cannot use Claude mode settings")
        from . import opencode
        opencode.send(slot, message, task_id)
    elif slot["provider"] == "pi":
        current = pi.query(slot)
        if current is None or current["state"] != "idle":
            raise RuntimeError("Pi no longer idle; reconcile before retry")
        pi.query(slot, message)
    elif slot.get("mode") == "print":
        claude.send_print(slot, message)
    else:
        claude.send_native(slot, message)
