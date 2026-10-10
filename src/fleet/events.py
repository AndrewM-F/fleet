"""Capture durable worker completions and relay bounded, acknowledged batches."""
import datetime
import json
from pathlib import Path
import time

from . import claude, paths

KINDS = {"review_ready", "task_uncertain", "task_unconsumed", "runner_error",
         "needs_attention", "possibly_stalled"}

CAPTURE_VERSION = 2


def message_text(message):
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(p["text"] for p in content
                     if isinstance(p, dict) and p.get("type") == "text"
                     and isinstance(p.get("text"), str))


def pi_completion(source, prompt, started, completed):
    """Bind text to the exact task prompt and never read past its observed end."""
    active = False
    latest = None
    if started is None or not source.exists():
        return None
    with source.open(errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            at = epoch(row.get("timestamp", message.get("timestamp")))
            role = message.get("role")
            if at is not None and at > completed:
                continue
            if role == "user":
                active = (at is not None and at >= started
                          and message_text(message) == prompt)
                latest = None
            elif active and role == "assistant":
                text = message_text(message)
                terminal = message.get("stopReason") in {"stop", "length", "error", "aborted"}
                latest = ((message, text, at) if at is not None and at >= started
                          and terminal and (text or message.get("stopReason") in {"error", "aborted"})
                          else None)
    return latest


def epoch(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else value
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def assistant_rows(path):
    if not path.exists():
        return
    with path.open(errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = row.get("message", {})
            if message.get("role") != "assistant":
                continue
            # Do not forward thinking blocks or tool results.
            text = "\n".join(p.get("text", "") for p in message.get("content", [])
                             if isinstance(p, dict) and p.get("type") == "text")
            if text:
                yield row, message, text


def capture(db, event_id, body, created):
    path = paths.RECEIPTS / f"{event_id}.json"
    legacy = None
    if path.exists():
        legacy = json.loads(path.read_text())
        if legacy.get("captureVersion") == CAPTURE_VERSION and not legacy.get("captureError"):
            return legacy
    sid = body.get("sessionId")
    tid = body.get("taskId")
    receipt = {"captureVersion": CAPTURE_VERSION,
               "eventId": event_id, "taskId": tid, "sessionId": sid,
               "completedAt": created, "state": body.get("state")}
    pi_slots, claude_slots = paths.slots()
    slot = next((s for s in pi_slots + claude_slots if s["sessionId"] == sid), None)
    task = db.execute("SELECT body FROM tasks WHERE id=?", (tid,)).fetchone()
    if not slot or not task:
        receipt["captureError"] = "Task or current roster entry unavailable"
    else:
        task = json.loads(task[0])
        receipt.update(model=slot["model"], effort=slot.get("effort", slot.get("thinking")),
                       cwd=slot["cwd"])
        if slot.get("mode") == "print":
            matches = []
            for request_file in claude.session_dir(sid).glob("*/request.json"):
                request = json.loads(request_file.read_text())
                if request.get("message") == task["message"] and request.get("sessionId") == sid:
                    matches.append(request_file.parent)
            if len(matches) == 1 and (matches[0] / "result.json").exists():
                result = json.loads((matches[0] / "result.json").read_text())
                receipt.update(summary=str(result.get("result", "")), usage=result.get("usage"),
                               isError=result.get("is_error"), subtype=result.get("subtype"),
                               correlation="exact task prompt and session",
                               resultFile=str(matches[0] / "result.json"))
            else:
                receipt["captureError"] = "No unique completed Claude turn matches this task"
        else:
            source = Path(slot["sessionFile"])
            consumed = None
            sent = None
            for kind, raw, at in db.execute(
                    "SELECT kind,body,created FROM events WHERE kind IN ('task_sent','task_consumed') AND created<=? ORDER BY id DESC",
                    (created,)):
                event = json.loads(raw)
                if event.get("taskId") != tid or event.get("sessionId") != sid:
                    continue
                if kind == "task_sent" and sent is None:
                    sent = at
                elif kind == "task_consumed" and consumed is None:
                    consumed = at
            # Socket forwarding acknowledgement can follow the actual prompt append.
            started = sent - 5 if sent is not None else (
                consumed - 30 if consumed is not None else None)
            latest = pi_completion(source, task["message"], started, created)
            if latest:
                message, text, at = latest
                receipt.update(summary=text, usage=message.get("usage"),
                               provider=message.get("provider"), messageTimestamp=at,
                               stopReason=message.get("stopReason"),
                               isError=message.get("stopReason") in {"error", "aborted"},
                               correlation="exact task prompt and observed completion boundary",
                               resultFile=str(source))
            else:
                receipt["captureError"] = "No terminal assistant text bound to this exact task prompt before completion"
    path.parent.mkdir(parents=True, exist_ok=True)
    if legacy is not None:
        legacy_path = path.with_name(f"{event_id}.legacy-v1.json")
        if not legacy_path.exists():
            legacy_path.write_text(json.dumps(legacy) + "\n")
        receipt["legacyReceiptFile"] = str(legacy_path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt) + "\n")
    tmp.replace(path)
    return receipt


def pending(db, after=0, limit=2):
    result = []
    for eid, kind, raw, created in db.execute(
            "SELECT id,kind,body,created FROM events WHERE acked=0 AND id>? ORDER BY id", (after,)):
        if kind not in KINDS:
            continue
        body = json.loads(raw)
        item = {"eventId": eid, "kind": kind, "body": body, "createdAt": created}
        if kind == "review_ready":
            receipt = capture(db, eid, body, created)
            item["completion"] = dict(receipt)
            summary = receipt.get("summary", "")
            item["completion"]["summary"] = summary[:4000]
            item["completion"]["summaryTruncated"] = len(summary) > 4000
            item["completion"]["receiptFile"] = str(paths.RECEIPTS / f"{eid}.json")
        result.append(item)
        if len(result) >= limit:
            break
    return result


def completions(db, command, after=0, limit=2, timeout=3600, ids=()):
    if command == "ack":
        db.executemany("UPDATE events SET acked=1 WHERE id=?", [(v,) for v in ids])
        db.commit()
        return {"acked": list(ids)}
    deadline = time.monotonic() + timeout
    while True:
        batch = pending(db, after, limit)
        if batch or command == "collect" or time.monotonic() >= deadline:
            return {"events": batch, "timeout": not batch and command == "wait"}
        time.sleep(1)
