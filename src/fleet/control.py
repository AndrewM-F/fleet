"""One mechanical fleet call: enqueue approved tasks, acknowledge receipts, wait."""
import fcntl
import json
import os
import time

from . import events, paths, roster
from .recovery import recovery_for_task, task_recovery_conflict
from .store import database


def enqueue(db, supplied):
    if not supplied:
        return []
    with paths.QUEUE_LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # The queue lock is taken first and the roster writer lock second,
        # which is the order every writer uses, so an enqueue cannot slip
        # between a removal's checks and its write. The roster is read inside
        # the locks: a roster a concurrent removal has already changed must
        # not be used to accept a task for a worker that is gone.
        with roster.roster_lock():
            slots = paths.all_slots()
            queue = (json.loads(paths.QUEUE.read_text()) if paths.QUEUE.exists()
                     else {"author": "root", "tasks": []})
            if queue.get("author") != "root":
                raise ValueError("Root queue required")
            by_id = {t["id"]: t for t in queue["tasks"]}
            added = []
            for task in supplied:
                if task.get("rootAuthorized") is not True or not task.get("id") or not task.get("message"):
                    raise ValueError("Explicit root task required")
                selectors = [k for k in ("sessionId", "lane", "previousTaskId") if task.get(k)]
                if len(selectors) != 1:
                    raise ValueError("Exactly one target selector required")
                sid = task.get("sessionId")
                if task.get("lane"):
                    matches = [s for s in slots if s.get("lane") == task["lane"]]
                    if len(matches) != 1:
                        raise ValueError("Lane is not unique")
                    sid = matches[0]["sessionId"]
                if task.get("previousTaskId"):
                    row = db.execute("SELECT sid FROM tasks WHERE id=?", (task["previousTaskId"],)).fetchone()
                    if not row:
                        raise ValueError("Previous task not found")
                    sid = row[0]
                matches = [s for s in slots if s["sessionId"] == sid]
                if len(matches) != 1:
                    raise ValueError("Target is not in current roster")
                old = by_id.get(task["id"])
                normalized = {"id": task["id"], "sessionId": sid, "message": task["message"],
                              "rootAuthorized": True}
                if task.get("previousTaskId"):
                    normalized["previousTaskId"] = task["previousTaskId"]
                recovery_id = task.get("recoveryId")
                if recovery_id:
                    statuses = ("authorized", "claimed", "completed") if old else ("authorized",)
                    if not task.get("previousTaskId") or not recovery_for_task(
                            db, recovery_id, task["id"], sid, task["previousTaskId"], statuses):
                        raise ValueError("Recovery task does not match an authorized durable handoff")
                    normalized["recoveryId"] = recovery_id
                elif db.execute("SELECT 1 FROM recoveries WHERE owner_task_id=? LIMIT 1",
                                (task["id"],)).fetchone():
                    raise ValueError("Task ID is reserved for a durable recovery handoff")
                conflict = task_recovery_conflict(db, normalized, sid, slots)
                if conflict:
                    raise ValueError(f"Task conflicts with active recovery {conflict}")
                if old and any(old.get(k) != v for k, v in normalized.items()):
                    raise ValueError("Task ID content conflict")
                if not old:
                    queue["tasks"].append(normalized)
                    by_id[task["id"]] = normalized
                added.append({"taskId": task["id"],
                              "target": task.get("lane") or task.get("previousTaskId") or sid,
                              "alreadyQueued": bool(old)})
            tmp = paths.QUEUE.with_name(paths.QUEUE.name + ".control.tmp")
            tmp.write_text(json.dumps(queue, indent=2) + "\n")
            os.replace(tmp, paths.QUEUE)
            return added


def cycle(request):
    if not isinstance(request, dict):
        raise ValueError("Request must be an object")
    ids = request.get("ack", [])
    if any(type(n) is not int or n < 1 for n in ids):
        raise ValueError("Invalid acknowledgement IDs")
    after = request.get("after", 0)
    limit = request.get("limit", 2)
    timeout = request.get("timeout", 3600)
    if (type(after) is not int or type(limit) is not int or not 1 <= limit <= 4
            or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 3600):
        raise ValueError("Invalid relay limits")
    minimum = request.get("minQueued", 0)
    if type(minimum) is not int or not 0 <= minimum <= 10:
        raise ValueError("Invalid queue minimum")
    db = database()
    queued = enqueue(db, request.get("tasks", []))
    db.executemany("UPDATE events SET acked=1 WHERE id=?", [(n,) for n in ids])
    db.commit()
    deadline = time.monotonic() + timeout
    while True:
        batch = events.pending(db, after, limit)
        runway = queue_runway(db, minimum)
        low = minimum > 0 and bool(runway["needsTasks"])
        if batch or low or time.monotonic() >= deadline:
            states = []
            for task in queued:
                row = db.execute("SELECT state FROM tasks WHERE id=?", (task["taskId"],)).fetchone()
                states.append(dict(task, state=row[0] if row else "awaiting_import"))
            return {"queued": states, "acked": ids, "events": batch,
                    "timeout": not batch and not low and timeout > 0,
                    "queueLow": low, "runway": runway}
        time.sleep(1)

def queue_runway(db, minimum=1):
    """Report approved successors; active and uncertain work never count as reserve."""
    slots = paths.all_slots()
    rows = {s["sessionId"]: {"sessionId": s["sessionId"], "queued": 0,
                            "active": 0, "uncertain": 0, "awaitingImport": 0}
            for s in slots}
    known = set()
    for task_id, sid, state in db.execute("SELECT id,sid,state FROM tasks"):
        known.add(task_id)
        row = rows.get(sid)
        if row is None:
            continue
        if state == "queued":
            row["queued"] += 1
        elif state in {"dispatching", "sent", "running"}:
            row["active"] += 1
        elif state == "uncertain":
            row["uncertain"] += 1
    # Atomic queue replacement makes a read see the old or new complete document.
    # Pending imports count as assigned reserve, but remain explicitly separate.
    queue = json.loads(paths.QUEUE.read_text())
    if queue.get("author") != "root":
        raise ValueError("Root queue required")
    seen = set()
    for task in queue["tasks"]:
        task_id = task.get("id")
        sid = task.get("sessionId")
        if (task_id and task_id not in known and task_id not in seen
                and task.get("rootAuthorized") is True and task.get("message")
                and sid in rows):
            rows[sid]["awaitingImport"] += 1
            seen.add(task_id)
    workers = sorted(rows.values(), key=lambda row: row["sessionId"])
    return {"workers": workers, "minimumQueued": minimum,
            "needsTasks": [row["sessionId"] for row in workers
                           if row["queued"] + row["awaitingImport"] < minimum]}
