"""Compact fleet snapshot and newest exact-task completion per worker. Never acknowledges."""
import argparse
import json
import time

from . import events, paths
from .store import database

ACTIVE = {"sent", "running", "uncertain"}
TERMINAL = {"finished", "cancelled", "failed"}


def report(db, slots, queue, after=0, summary_limit=1600, capture=events.capture, now=None):
    now = time.time() if now is None else now
    if after < 0 or not 0 <= summary_limit <= 12000:
        raise ValueError("Invalid report bounds")
    roster = {slot["sessionId"]: slot for slot in slots}
    tasks = {row[0]: {"sid": row[1], "state": row[2]}
             for row in db.execute("SELECT id,sid,state FROM tasks")}
    snapshots = {row[0]: (json.loads(row[1]), row[2])
                 for row in db.execute("SELECT sid,body,observed FROM snapshots")}
    latest, pending = {}, {sid: [] for sid in roster}
    for eid, raw, created, acked in db.execute(
            "SELECT id,body,created,acked FROM events WHERE kind='review_ready' ORDER BY id DESC"):
        body = json.loads(raw)
        sid = body.get("sessionId")
        if sid not in roster:
            continue
        latest.setdefault(sid, (eid, body, created, acked))
        if not acked:
            pending[sid].append(eid)
    awaiting = {sid: 0 for sid in roster}
    lanes = {slot.get("lane"): sid for sid, slot in roster.items() if slot.get("lane")}
    for item in queue.get("tasks", []):
        if item.get("id") in tasks:
            continue
        sid = item.get("sessionId") or lanes.get(item.get("lane"))
        if sid is None and item.get("previousTaskId") in tasks:
            sid = tasks[item["previousTaskId"]]["sid"]
        if sid in awaiting:
            awaiting[sid] += 1
    workers = []
    for sid, slot in roster.items():
        owned = [(tid, task["state"]) for tid, task in tasks.items() if task["sid"] == sid]
        snap, observed = snapshots.get(sid, ({}, None))
        state = snap.get("state")
        if not isinstance(state, str):
            state = snap.get("status") if isinstance(snap.get("status"), str) else "unknown"
        row = {"sessionId": sid, "lane": slot.get("lane"), "model": slot.get("model"),
               "effort": slot.get("effort", slot.get("thinking")), "state": state,
               "observedAgeSeconds": None if observed is None else max(0, round(now-observed)),
               "activeTasks": [{"id": tid, "state": state} for tid, state in owned if state in ACTIVE],
               "queued": sum(state == "queued" for _, state in owned),
               "awaitingImport": awaiting[sid], "pendingReviewCount": len(pending[sid]),
               "oldestPendingReviewIds": list(reversed(pending[sid]))[:10]}
        if sid in latest:
            eid, body, created, acked = latest[sid]
            row["latestCompletion"] = {"eventId": eid, "taskId": body.get("taskId"),
                                       "createdAt": created, "acked": bool(acked)}
            if not acked and eid > after:
                try:
                    receipt = capture(db, eid, body, created)
                    summary = receipt.get("summary", "")
                    if not isinstance(summary, str):
                        summary = ""
                    row["latestCompletion"].update(
                        summary=summary[:summary_limit], summaryLength=len(summary),
                        summaryTruncated=len(summary) > summary_limit,
                        captureError=receipt.get("captureError"),
                        isError=receipt.get("isError"), stopReason=receipt.get("stopReason"),
                        receiptFile=str(paths.RECEIPTS / f"{eid}.json"))
                except Exception as error:
                    row["latestCompletion"]["captureError"] = type(error).__name__
        workers.append(row)
    newest = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
    return {"observedAt": now, "latestEventId": newest, "workers": workers,
            "note": "Latest completion per worker only; pending counts preserve older review backlog. No acknowledgements."}


def incremental_report(db, slots, queue, after=0, summary_limit=1000, limit=5,
                       capture=events.capture, now=None):
    """Page every actionable event through a fixed watermark. Never acknowledges."""
    now = time.time() if now is None else now
    if after < 0 or not 0 <= summary_limit <= 12000 or not 1 <= limit <= 50:
        raise ValueError("Invalid incremental report bounds")
    # Capture BEFORE reading rows: events arriving while receipts are captured belong
    # to the next page, not a silently advanced cursor.
    watermark = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
    roster = {slot["sessionId"]: slot for slot in slots}
    task_rows = list(db.execute("SELECT id,sid,state FROM tasks"))
    tasks = {tid: {"sid": sid, "state": state} for tid, sid, state in task_rows}
    snapshots = {sid: json.loads(raw) for sid, raw in
                 db.execute("SELECT sid,body FROM snapshots")}
    counts = {}
    for sid in roster:
        snap = snapshots.get(sid, {})
        state = snap.get("state")
        if not isinstance(state, str):
            state = snap.get("status") if isinstance(snap.get("status"), str) else "unknown"
        counts[state] = counts.get(state, 0) + 1
    rows = list(db.execute(
        "SELECT id,kind,body,created,acked FROM events WHERE id>? AND id<=? "
        "AND kind IN ('review_ready','runner_error','task_uncertain',"
        "'task_unconsumed','needs_attention') ORDER BY id LIMIT ?",
        (after, watermark, limit + 1)))
    has_more = len(rows) > limit
    output = []
    for eid, kind, raw, created, acked in rows[:limit]:
        body = json.loads(raw)
        sid = body.get("sessionId")
        slot = roster.get(sid, {})
        item = {"eventId": eid, "kind": kind, "sessionId": sid,
                "taskId": body.get("taskId"), "createdAt": created,
                "acked": bool(acked), "model": slot.get("model"),
                "effort": slot.get("effort", slot.get("thinking"))}
        if kind == "review_ready":
            try:
                receipt = capture(db, eid, body, created)
                summary = receipt.get("summary", "")
                if not isinstance(summary, str):
                    summary = ""
                item.update(summary=summary[:summary_limit],
                            summaryLength=len(summary),
                            summaryTruncated=len(summary) > summary_limit,
                            captureError=receipt.get("captureError"),
                            isError=receipt.get("isError"),
                            stopReason=receipt.get("stopReason"),
                            receiptFile=str(paths.RECEIPTS / f"{eid}.json"))
            except Exception as error:
                item["captureError"] = type(error).__name__
        else:
            # Avoid copying arbitrary event payloads or raw provider output.
            for key in ("reasonCode", "errorCode", "state"):
                value = body.get(key)
                if isinstance(value, (str, int, bool)):
                    item[key] = value[:200] if isinstance(value, str) else value
        output.append(item)
    awaiting = sum(1 for item in queue.get("tasks", []) if item.get("id") not in tasks)
    return {"observedAt": now, "latestEventId": watermark,
            "nextAfter": output[-1]["eventId"] if has_more else max(after, watermark),
            "hasMore": has_more, "stateCounts": counts,
            "activeTasks": [{"id": tid, "sessionId": sid, "state": state}
                            for tid, sid, state in task_rows if sid in roster and state in ACTIVE],
            "queued": sum(state == "queued" for _, sid, state in task_rows if sid in roster),
            "awaitingImport": awaiting, "events": output,
            "note": "Every actionable event is paged in order. Resume from nextAfter. No acknowledgements."}


def wait_for_event(db, after, timeout, clock=time.monotonic, sleep=time.sleep):
    """Wait in Python, without model polling, for a completion or actionable error."""
    deadline = clock() + timeout
    while timeout:
        wake = db.execute(
            "SELECT id FROM events WHERE id>? AND kind IN "
            "('review_ready','runner_error','task_uncertain','task_unconsumed','needs_attention') LIMIT 1",
            (after,)).fetchone()
        if wake or clock() >= deadline:
            return
        sleep(min(2, max(0, deadline - clock())))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--summary-limit", type=int, default=1600)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)
    if not 0 <= args.wait_seconds <= 900:
        parser.error("--wait-seconds must be between 0 and 900")
    if not paths.DB.exists():
        raise RuntimeError("Fleet state unavailable")
    slots = paths.all_slots()
    queue = json.loads(paths.QUEUE.read_text())
    db = database()
    try:
        wait_for_event(db, args.after, args.wait_seconds)
        queue = json.loads(paths.QUEUE.read_text())
        result = (incremental_report(db, slots, queue, args.after, args.summary_limit, args.limit)
                  if args.incremental else report(db, slots, queue, args.after, args.summary_limit))
        print(json.dumps(result))
    finally:
        db.close()


if __name__ == "__main__":
    main()
