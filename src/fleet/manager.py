"""Observe workers, import root-authored tasks, dispatch, and record state every 25s."""
import fcntl
import json
import os
from pathlib import Path
import signal
import time

from . import claude, opencode, paths, pi, pr_queue, recovery
from .dispatch import dispatch, send
from .store import BUSY, TERMINAL, database, emit
from .util import atomic
from .watchdog import check_queue

STOP = False


def observe(db, sid, state, now=None):
    now = time.time() if now is None else now
    previous = db.execute("SELECT body,changed,stalled FROM snapshots WHERE sid=?",
                          (sid,)).fetchone()
    old = json.loads(previous[0]) if previous else None
    changed = now if old != state else previous[1]
    stalled = previous[2] if old == state else 0
    if old is None or old.get("state") != state["state"]:
        kind = "needs_attention" if state["state"] in {"unavailable", "blocked", "failed", "error"} else "state_changed"
        emit(db, kind, sessionId=sid, previous=old, current=state)
    if state["state"] in BUSY and now - changed > 1200 and not stalled:
        emit(db, "possibly_stalled", sessionId=sid, unchangedSeconds=int(now-changed))
        stalled = 1
    db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)",
               (sid, json.dumps(state), changed, now, stalled))
    for task_id, raw, phase, updated in db.execute(
            "SELECT id,body,state,updated FROM tasks WHERE sid=? AND state IN ('sent','running')",
            (sid,)).fetchall():
        task = json.loads(raw)
        progressed = state.get("progress") != task.get("baselineProgress")
        if phase == "sent" and (state["state"] in BUSY or progressed):
            phase = "running"
            db.execute("UPDATE tasks SET state='running',updated=? WHERE id=?", (now, task_id))
            emit(db, "task_consumed", taskId=task_id, sessionId=sid)
        if phase == "running" and state["state"] in TERMINAL:
            db.execute("UPDATE tasks SET state='finished',updated=? WHERE id=?", (now, task_id))
            task_data = json.loads(db.execute("SELECT body FROM tasks WHERE id=?",
                                              (task_id,)).fetchone()[0])
            if task_data.get("recoveryId"):
                db.execute("UPDATE recoveries SET status='completed',updated=? "
                           "WHERE id=? AND status='claimed' AND owner_task_id=?",
                           (now, task_data["recoveryId"], task_id))
            emit(db, "review_ready", taskId=task_id, sessionId=sid, state=state["state"])
        elif phase == "sent" and now - updated > 90:
            db.execute("UPDATE tasks SET state='uncertain',updated=? WHERE id=?", (now, task_id))
            emit(db, "task_unconsumed", taskId=task_id, sessionId=sid)
    db.commit()


def import_queue(db, slots, queue=None):
    queue_path = Path(queue or paths.QUEUE)
    if not queue_path.exists():
        return
    data = json.loads(queue_path.read_text())
    if data.get("author") != "root":
        raise ValueError("Queue must be authored by root")
    for task in data["tasks"]:
        sid = task["sessionId"]
        existing = db.execute("SELECT body FROM tasks WHERE id=?", (task["id"],)).fetchone()
        if existing:
            saved = json.loads(existing[0])
            if any(saved.get(k) != task.get(k) for k in
                   ("sessionId", "message", "previousTaskId", "recoveryId")):
                raise ValueError("Task ID reused with different content")
            continue
        if task.get("rootAuthorized") is not True or sid not in slots or not task.get("message"):
            raise ValueError("Unapproved or unknown task target")
        if task.get("recoveryId") and not recovery.recovery_for_task(
                db, task["recoveryId"], task["id"], sid, task.get("previousTaskId")):
            pending = db.execute("SELECT status FROM recoveries WHERE id=?",
                                 (task["recoveryId"],)).fetchone()
            if pending and pending[0] == "preparing":
                continue
            raise ValueError("Recovery task does not match an authorized durable handoff")
        conflict = recovery.task_recovery_conflict(db, task, sid)
        if conflict:
            raise ValueError(f"Queued task conflicts with active recovery {conflict}")
        db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                   (task["id"], sid, str(Path(slots[sid]["cwd"]).resolve()),
                    json.dumps(task), "queued", time.time()))
    db.commit()


def collect_states(pi_slots, claude_slots, opencode_slots=()):
    states = {}
    for slot in pi_slots:
        try:
            state = pi.query(slot)
            if state is None:
                raise RuntimeError("Pi response timed out")
        except Exception as error:
            state = {"state": "unavailable", "error": str(error), "cwd": slot["cwd"]}
        states[slot["sessionId"]] = state
    try:
        # Once cut over, managed turns do not depend on the native bg daemon.
        rows = claude.claude_rows() if any(s.get("mode") != "print" for s in claude_slots) else {}
        for sid, row in rows.items():
            states[sid] = claude.claude_state(row)
        for slot in claude_slots:
            if slot.get("mode") == "print":
                states[slot["sessionId"]] = claude.print_state(slot)
            elif slot["sessionId"] not in rows:
                states[slot["sessionId"]] = {"state": "unavailable", "cwd": slot["cwd"]}
    except Exception as error:
        states["claude-monitor"] = {"state": "unavailable", "error": str(error)}
    for slot in opencode_slots:
        try:
            state = opencode.state(slot)
        except Exception as error:
            state = {"state": "unavailable", "error": str(error), "cwd": slot["cwd"]}
        states[slot["sessionId"]] = state
    return states


def recover(db):
    """A task left 'dispatching' may or may not have been delivered: never resend it."""
    for (task_id,) in db.execute("SELECT id FROM tasks WHERE state='dispatching'").fetchall():
        db.execute("UPDATE tasks SET state='uncertain' WHERE id=?", (task_id,))
        emit(db, "task_uncertain", taskId=task_id, reason="runner restarted during dispatch")
    db.commit()


def run():
    global STOP
    paths.DATA.mkdir(parents=True, exist_ok=True)
    with paths.MANAGER_LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = database()
        recover(db)

        def stop(*args):
            global STOP
            STOP = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not STOP:
            started = time.time()
            try:
                pi_slots, claude_slots = paths.slots()
                opencode_slots = paths.opencode_slots()
                slots = {s["sessionId"]: s for s in pi_slots + claude_slots + opencode_slots}
                states = collect_states(pi_slots, claude_slots, opencode_slots)
                for sid, state in states.items():
                    observe(db, sid, state)
                import_queue(db, slots)
                pr_queue.tick(db, slots, states)
                import_queue(db, slots)
                dispatch(db, slots, states, send, paths.PAUSE.exists(),
                         int(paths.config().get("maxConcurrent", 0)))
                check_queue(db, slots, states, paths.PAUSE.exists(),
                            minimum=int(paths.config().get("minQueuedPerWorker", 1)))
                atomic(paths.STATUS, {"pid": os.getpid(), "updatedAt": time.time(),
                                      "paused": paths.PAUSE.exists(), "sessions": states,
                                      "queue": list(db.execute("SELECT id,sid,state FROM tasks"))})
            except Exception as error:
                emit(db, "runner_error", error=str(error))
            for _ in range(max(1, int(25-(time.time()-started)))):
                if STOP:
                    break
                time.sleep(1)


def wait(timeout):
    db = database()
    deadline = time.monotonic() + timeout
    while True:
        row = db.execute("SELECT id,kind,body,created FROM events WHERE acked=0 ORDER BY id LIMIT 1").fetchone()
        if row:
            return {"eventId": row[0], "kind": row[1], "body": json.loads(row[2]), "createdAt": row[3]}
        if time.monotonic() >= deadline:
            return {"timeout": True}
        time.sleep(.5)


def ack(event_id):
    db = database()
    db.execute("UPDATE events SET acked=1 WHERE id=?", (event_id,))
    db.commit()
