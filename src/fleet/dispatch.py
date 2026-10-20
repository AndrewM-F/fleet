"""Dispatch queued tasks at most once to idle, non-conflicting workers."""
import json
from contextlib import nullcontext
from pathlib import Path
import subprocess
import time

from . import claude, pi
from . import holds, model_selection, recovery
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
        task = json.loads(raw)
        if holds.is_held(db, task_id):
            continue  # An operator hold pauses this task; never consume or cancel it.
        recovery_id = task.get("recoveryId")
        recovering_error = (slot.get("provider") == "opencode"
                            and state.get("state") == "error" and recovery_id)
        if recovery_id:
            record = recovery.recovery_for_task(
                db, recovery_id, task_id, sid, task.get("previousTaskId"))
            if not record:
                continue
            try:
                recovery.validate_owned_worktree(db, record, slots)
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                already_reported = False
                for (body,) in db.execute(
                        "SELECT body FROM events WHERE kind='needs_attention'").fetchall():
                    try:
                        if json.loads(body).get("taskId") == task_id:
                            already_reported = True
                            break
                    except (TypeError, json.JSONDecodeError):
                        continue
                if not already_reported:
                    emit(db, "needs_attention", taskId=task_id, sessionId=sid,
                         error=f"Owned worktree recovery check failed: {error}")
                continue
        if recovery_id and not recovering_error:
            continue
        if state.get("state") not in {"idle", "done", "completed", "stopped"} \
                and not recovering_error:
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
        task["baselineProgress"] = state.get("progress")
        db.commit()
        # A model selection and a claim/launch share one lock, so a task can
        # never be claimed under one model and launched under another. The model
        # the claim uses is recorded in the task body and is the model the
        # launch uses.
        is_opencode = slot.get("provider") == "opencode"
        model_lock = model_selection.model_lock() if is_opencode else nullcontext()
        with model_lock:
            claim_model = None
            if is_opencode:
                try:
                    claim_model = model_selection.claim_model(slot)
                except ValueError:
                    # A worker that declares no model cannot be launched. The
                    # launch path refuses it; the claim keeps its existing
                    # behaviour so nothing else is hidden by this check.
                    claim_model = None
                if claim_model:
                    task["model"] = claim_model
            db.execute("BEGIN IMMEDIATE")
            claimed = db.execute(
                "UPDATE tasks SET state='dispatching',body=?,updated=? WHERE id=? AND state='queued' "
                "AND NOT EXISTS (SELECT 1 FROM task_holds WHERE task_id=? AND status='held')",
                (json.dumps(task), time.time(), task_id, task_id))
            recovery_record = None
            if claimed.rowcount == 1 and recovery_id:
                recovery_record = recovery.claim_recovery(
                    db, recovery_id, task_id, sid, task.get("previousTaskId"))
                if recovery_record is None:
                    db.rollback()
                    continue
            db.commit()  # Crash after this point means uncertain delivery; never resend.
            if claimed.rowcount != 1:
                continue  # An operator cancelled this task after the queue snapshot.
            try:
                if is_opencode:
                    if recovery_record:
                        sender(slot, task["message"], task_id=task_id,
                               recovery=recovery_record, model=claim_model)
                    else:
                        sender(slot, task["message"], task_id=task_id, model=claim_model)
                else:
                    sender(slot, task["message"])
            except Exception as error:
                db.execute("UPDATE tasks SET state='uncertain',updated=? WHERE id=?", (time.time(), task_id))
                emit(db, "task_uncertain", taskId=task_id, sessionId=sid, error=str(error))
            else:
                db.execute("UPDATE tasks SET state='sent',updated=? WHERE id=?", (time.time(), task_id))
                emit(db, "task_sent", taskId=task_id, sessionId=sid)
            db.commit()
        active += 1
        states[sid] = dict(state, state="queued")


def send(slot, message, task_id=None, recovery=None, model=None):
    if slot.get("provider") == "opencode":
        if slot.get("mode") not in (None, "opencode"):
            raise ValueError("OpenCode workers cannot use Claude mode settings")
        from . import opencode
        if recovery:
            opencode.send(slot, message, task_id, recovery, model=model)
        else:
            opencode.send(slot, message, task_id, model=model)
    elif slot["provider"] == "pi":
        current = pi.query(slot)
        if current is None or current["state"] != "idle":
            raise RuntimeError("Pi no longer idle; reconcile before retry")
        pi.query(slot, message)
    elif slot.get("mode") == "print":
        claude.send_print(slot, message)
    else:
        claude.send_native(slot, message)
