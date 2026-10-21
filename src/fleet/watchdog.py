"""Durable queue-starvation alerts; never invents tasks or changes permissions."""
import json
import time

from .store import BUSY, emit

IDLE = {"idle", "done", "completed", "stopped"}


def _transition(db, category, payload, now, repeat_after):
    signature = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    previous = db.execute(
        "SELECT body,created FROM events WHERE kind IN ('needs_attention','state_changed') "
        "AND json_extract(body,'$.category')=? ORDER BY id DESC LIMIT 1",
        (category,)).fetchone()
    old = json.loads(previous[0]) if previous else None
    low = bool(payload["workers"])
    if not low and (old is None or not old.get("workers")):
        return None
    if old and old.get("signature") == signature and now - previous[1] < repeat_after:
        return None
    return emit(db, "needs_attention" if low else "state_changed",
                category=category, signature=signature,
                status="blocked" if low else "restored", **payload)


def check_queue(db, slots, states, paused=False, minimum=1, now=None,
                repeat_after=600, blocked_after=120, runway=None):
    """Warn before reserve runs dry and when approved queued work cannot dispatch.

    This observes only. Uncertain deliveries, disabled workers, pause state,
    task authorization and review decisions remain owned by existing controls.
    Alerts are transition-deduplicated in the event store and repeat after ten
    minutes while unresolved, including after manager restart.
    """
    if paused or minimum == 0:
        return []
    if type(minimum) is not int or not 1 <= minimum <= 10:
        raise ValueError("Queue reserve must be between 1 and 10")
    now = time.time() if now is None else now
    if runway is None:
        from .control import queue_runway
        runway = queue_runway(db, minimum)
    eligible = {
        sid for sid, slot in slots.items()
        if (slot.get("provider") not in {"claude", "opencode"}
            or slot.get("dispatchEnabled", False))
        and not slot.get("draining")
        and states.get(sid, {}).get("state") in IDLE | BUSY
    }
    missing = []
    for row in runway["workers"]:
        sid = row["sessionId"]
        if sid not in eligible or row.get("uncertain", 0):
            continue
        reserve = row["queued"] + row["awaitingImport"]
        if reserve < minimum:
            missing.append({
                "sessionId": sid, "reserve": reserve,
                "idle": states[sid]["state"] in IDLE and row["active"] == 0,
            })
    missing.sort(key=lambda row: row["sessionId"])
    review_ids = []
    waiting = {row["sessionId"] for row in missing}
    seen = set()
    if waiting:
        for eid, raw in db.execute(
                "SELECT id,body FROM events WHERE kind='review_ready' AND acked=0 ORDER BY id DESC"):
            sid = json.loads(raw).get("sessionId")
            if sid in waiting and sid not in seen:
                review_ids.append(eid)
                seen.add(sid)
                if seen == waiting:
                    break
    alerts = []
    event = _transition(db, "queue_runway",
                        {"workers": missing, "minimumQueued": minimum,
                         "unacknowledgedReviewEvents": sorted(review_ids)},
                        now, repeat_after)
    if event is not None:
        alerts.append(event)

    blocked = []
    for tid, sid, updated in db.execute(
            "SELECT id,sid,updated FROM tasks WHERE state='queued' ORDER BY id"):
        if (sid in eligible and states[sid]["state"] in IDLE
                and now - updated >= blocked_after):
            blocked.append({"sessionId": sid, "taskId": tid})
    event = _transition(db, "dispatch_blocked",
                        {"workers": blocked, "minimumWaitSeconds": blocked_after},
                        now, repeat_after)
    if event is not None:
        alerts.append(event)
    return alerts
