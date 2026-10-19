"""Durable, single-use recovery authorization for a verified failed OpenCode turn."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from . import opencode, paths

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RATE_LIMIT = re.compile(
    r"rate.?limit|too many requests|\b(?:403|429)\b|forbidden|access denied|permission denied|"
    r"quota exceeded|resource exhausted|insufficient credits|billing|"
    r"account.{0,20}(?:disabled|restricted)",
    re.I,
)


def worktree_snapshot(cwd):
    """Fingerprint branch/head, Git status/diff, and non-ignored untracked files.

    This is not a backup or a complete partial-state capture: ignored contents,
    nested repository/submodule contents and intermediate index blobs are omitted.
    """
    root = Path(cwd).expanduser().resolve(strict=True)

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              stdin=subprocess.DEVNULL, capture_output=True).stdout

    top_level = Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top_level != root:
        raise ValueError("Recovery path must be the root of its owned Git worktree")
    branch = git("branch", "--show-current").decode().strip()
    head = git("rev-parse", "HEAD").decode().strip()
    status = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    diff = git("diff", "--binary", "HEAD", "--")
    digest = hashlib.sha256()
    for label, value in ((b"branch", branch.encode()), (b"head", head.encode()),
                         (b"status", status), (b"diff", diff)):
        digest.update(label + b"\0" + value + b"\0")
    for entry in status.split(b"\0"):
        if entry.startswith(b"?? "):
            raw_path = entry[3:]
            path = root / os.fsdecode(raw_path)
            digest.update(b"untracked\0" + raw_path + b"\0")
            if path.is_symlink():
                digest.update(b"symlink\0" + os.fsencode(os.readlink(path)))
            elif path.is_file():
                digest.update(b"file\0")
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                digest.update(b"other\0")
            digest.update(b"\0")
    if not branch:
        raise ValueError("Recovery requires a named Git branch, not detached HEAD")
    return {"branch": branch, "head": head, "worktreeSha256": digest.hexdigest()}


def verify_failed_turn(session_id, failed_task_id, turn, cwd, model):
    """Verify the durable pointer, error receipt, cleanup and any recorded PID identity."""
    pointer = opencode.session_dir(session_id) / "current.json"
    if not pointer.is_file():
        raise ValueError("No managed OpenCode turn exists for this worker")
    launch = json.loads(pointer.read_text())
    if (launch.get("turn") != turn or launch.get("taskId") != failed_task_id
            or launch.get("sessionId") != session_id or launch.get("cwd") != cwd
            or launch.get("model") != model):
        raise ValueError("Current OpenCode turn identity differs from the requested failed task")
    state_path = opencode.turn_dir(session_id, turn) / "state.json"
    result_path = opencode.turn_dir(session_id, turn) / "result.json"
    if not state_path.is_file() or not result_path.is_file():
        raise ValueError("OpenCode turn state/result is incomplete; recovery is blocked")
    state = json.loads(state_path.read_text())
    result = json.loads(result_path.read_text())
    identity = (state.get("turn", turn) == turn
                and state.get("taskId") == failed_task_id
                and state.get("sessionId") == session_id and state.get("cwd") == cwd
                and state.get("model") == model and state.get("state") == "error"
                and state.get("cleanup") == "confirmed"
                and isinstance(state.get("finishedAt"), (int, float))
                and result.get("turn", turn) == turn
                and result.get("taskId") == failed_task_id
                and result.get("sessionId") == session_id and result.get("cwd") == cwd
                and result.get("state") == "error" and result.get("cleanup") == "confirmed"
                and isinstance(result.get("finishedAt"), (int, float)))
    if not identity:
        raise ValueError("OpenCode error result identity or cleanup is not verified")
    failures = result.get("toolFailures") or []
    if not isinstance(failures, list):
        raise ValueError("OpenCode error receipt has an invalid tool-failure list")
    failure_text = " ".join([str(result.get("error") or ""),
                             *(str(item.get("error") or "") for item in failures
                               if isinstance(item, dict))])
    if _RATE_LIMIT.search(failure_text):
        raise ValueError("Rate-limit/quota failures cannot be recovered by this operation")
    process_identity = result.get("managedProcessIdentity")
    if process_identity is not None:
        if not isinstance(process_identity, dict) or not process_identity.get("pid") \
                or not process_identity.get("pgid"):
            raise ValueError("OpenCode error receipt has an invalid native process identity")
        if (opencode.identity_matches(process_identity)
                or opencode._group_exists(process_identity["pgid"])):
            raise ValueError("OpenCode native process group is still live")
    return launch, state, result


def _queue_lock():
    Path(paths.QUEUE_LOCK).parent.mkdir(parents=True, exist_ok=True)
    handle = Path(paths.QUEUE_LOCK).open("a")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _queue_has_task(task_id):
    if not Path(paths.QUEUE).exists():
        return False
    queue = json.loads(Path(paths.QUEUE).read_text())
    if queue.get("author") != "root" or not isinstance(queue.get("tasks"), list):
        raise ValueError("Root task queue is invalid; recovery is blocked")
    return any(task.get("id") == task_id for task in queue["tasks"])


def _same_record(row, values):
    fields = ("id", "failed_sid", "failed_turn", "failed_task_id", "owner_sid",
              "owner_task_id", "ownership_task_id", "cwd", "owned_worktree",
              "branch", "head", "worktree_sha256", "note")
    return all(row[key] == values[key] for key in fields)


def _task_message(db, task_id, session_id, cwd):
    row = db.execute("SELECT sid,cwd,state,body FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row or row[0] != session_id or str(Path(row[1]).resolve()) != cwd:
        raise ValueError("Ownership task must belong to the same native worker and CWD")
    if row[2] != "finished":
        raise ValueError("Ownership task must be terminal before recovery")
    try:
        message = json.loads(row[3]).get("message", "")
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Ownership task record is invalid") from error
    if not isinstance(message, str):
        raise ValueError("Ownership task record has no message")
    return message


def _resolve(value):
    """Resolve a path for identity comparison; empty values stay empty."""
    return str(Path(value).expanduser().resolve()) if value else ""


def _configured_cwds(slots=None):
    """Map every configured session to its structured, resolved CWD.

    Accepts a slot list or a ``{sessionId: slot}`` mapping, as produced by
    ``paths.all_slots()`` and the manager roster. Falls back to the OpenCode
    roster so recovery callers that already read it stay consistent.
    """
    if slots is None:
        try:
            slots = paths.opencode_slots()
        except (FileNotFoundError, ValueError):
            slots = []
    if isinstance(slots, dict):
        slots = slots.values()
    result = {}
    for slot in slots or []:
        sid, cwd = slot.get("sessionId"), slot.get("cwd")
        if sid and cwd:
            result[sid] = _resolve(cwd)
    return result


def _claims_worktree(tree, *, cwd=None, sid=None, message="", configured=None):
    """Does this task claim ``tree`` through structured or supplemental evidence?

    Structured ownership wins: the task's durable ``tasks.cwd`` column and its
    target session's configured CWD. A path substring in the free-text message is
    kept only as conservative supplemental evidence, never as the sole basis.
    """
    if cwd and _resolve(cwd) == tree:
        return True
    if configured and sid and configured.get(sid) == tree:
        return True
    return bool(tree and message and tree in message)


def _active_task_for_worktree(db, owned_worktree, configured, exclude_task_id=None):
    tree = _resolve(owned_worktree)
    rows = db.execute("SELECT id,sid,cwd,body FROM tasks WHERE state IN "
                      "('queued','dispatching','sent','running','uncertain')").fetchall()
    for task_id, sid, cwd, body in rows:
        if task_id == exclude_task_id:
            continue
        try:
            message = json.loads(body).get("message", "")
        except (TypeError, json.JSONDecodeError):
            message = ""
        if not isinstance(message, str):
            message = ""
        if _claims_worktree(tree, cwd=cwd, sid=sid, message=message, configured=configured):
            return task_id
    return None


def _queued_task_for_worktree(db, owned_worktree, configured, exclude_task_id=None):
    if not Path(paths.QUEUE).exists():
        return None
    queue = json.loads(Path(paths.QUEUE).read_text())
    if queue.get("author") != "root" or not isinstance(queue.get("tasks"), list):
        raise ValueError("Root task queue is invalid; recovery is blocked")
    tree = _resolve(owned_worktree)
    for task in queue["tasks"]:
        task_id = task.get("id")
        if task_id == exclude_task_id:
            continue
        if not _claims_worktree(tree, sid=task.get("sessionId"),
                                message=str(task.get("message") or ""),
                                configured=configured):
            continue
        row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row[0] in {"queued", "dispatching", "sent", "running", "uncertain"}:
            return task_id or "unknown"
    return None


def _validate_recovery_mapping(db, record, configured):
    """Bind the recovery's native CWD/worker to structured task and roster fields."""
    row = db.execute("SELECT sid,cwd,state FROM tasks WHERE id=?",
                     (record["ownership_task_id"],)).fetchone()
    if not row or row[0] != record["owner_sid"] or _resolve(row[1]) != _resolve(record["cwd"]):
        raise ValueError("Recovery native CWD/worker does not match the ownership task")
    if row[2] != "finished":
        raise ValueError("Ownership task must be terminal before recovery")
    expected = configured.get(record["owner_sid"])
    if expected and expected != _resolve(record["cwd"]):
        raise ValueError("Recovery native CWD does not match the configured session cwd")


def validate_owned_worktree(db, record, slots=None):
    """Recheck task provenance and exclusivity before dispatching a successor."""
    worktree = record["owned_worktree"]
    configured = _configured_cwds(slots)
    _validate_recovery_mapping(db, record, configured)
    message = _task_message(db, record["ownership_task_id"], record["owner_sid"], record["cwd"])
    if worktree not in message or record["branch"] not in message:
        raise ValueError("Owned worktree/branch no longer match the owner task record")
    snapshot = worktree_snapshot(worktree)
    if (snapshot["branch"] != record["branch"] or snapshot["head"] != record["head"]
            or snapshot["worktreeSha256"] != record["worktree_sha256"]):
        raise ValueError("Owned worktree changed after recovery authorization")
    conflict = _active_task_for_worktree(db, worktree, configured, record["owner_task_id"])
    if conflict:
        raise ValueError(f"Another active task owns the explicit worktree: {conflict}")
    conflict = _queued_task_for_worktree(db, worktree, configured, record["owner_task_id"])
    if conflict and conflict != record["owner_task_id"]:
        raise ValueError(f"Another queued task owns the explicit worktree: {conflict}")
    other = db.execute("SELECT id FROM recoveries WHERE owned_worktree=? AND id<>? "
                       "AND status IN ('preparing','authorized','claimed') LIMIT 1",
                       (worktree, record["id"])).fetchone()
    if other:
        raise ValueError(f"Another recovery owns the explicit worktree: {other[0]}")


def task_recovery_conflict(db, task, sid, slots=None):
    """Return a recovery reserving this session or a worktree the task claims.

    A task claims a worktree through its structured CWD and its target session's
    configured CWD, not only through a path string in its message.
    """
    valid = recovery_for_task(db, task.get("recoveryId"), task.get("id"), sid,
                              task.get("previousTaskId"),
                              statuses=("authorized", "claimed", "completed")) \
        if task.get("recoveryId") else None
    if valid:
        return None
    configured = _configured_cwds(slots)
    task_cwd = task.get("cwd")
    message = str(task.get("message") or "")
    rows = db.execute("SELECT id,owner_sid,owned_worktree,cwd FROM recoveries "
                      "WHERE status IN ('preparing','authorized','claimed')").fetchall()
    for recovery_id, owner_sid, worktree, native_cwd in rows:
        if sid == owner_sid:
            return recovery_id
        for reserved in (worktree, native_cwd):
            if reserved and _claims_worktree(_resolve(reserved), cwd=task_cwd, sid=sid,
                                             message=message, configured=configured):
                return recovery_id
    return None


def authorize_opencode(db, *, session_id, failed_task_id, turn, recovery_id,
                       successor_task_id, ownership_task_id, owned_worktree,
                       branch, head, worktree_sha256, note):
    """Record a verified error-turn handoff without editing task or turn history.

    The successor remains root-queued work. This operation only creates its
    single-use authorization; it never starts a model turn or changes a hold.
    """
    fields = (session_id, failed_task_id, turn, recovery_id, successor_task_id,
              ownership_task_id, owned_worktree, branch, head, note)
    if any(not isinstance(value, str) or not value.strip() for value in fields):
        raise ValueError("Recovery identifiers, branch, and note must be non-empty strings")
    if not _SHA256.fullmatch(worktree_sha256 or ""):
        raise ValueError("worktree_sha256 must be a lowercase SHA-256 digest")
    all_slots = paths.opencode_slots()
    slots = [s for s in all_slots if s.get("sessionId") == session_id]
    if len(slots) != 1:
        raise ValueError("Recovery target must be one existing OpenCode slot")
    slot = opencode.verify_slot(slots[0])
    if not slot.get("dispatchEnabled", False):
        raise ValueError("OpenCode dispatch is disabled; recovery does not change that hold")

    # Structured ownership: bind the native CWD/worker to the roster and the
    # ownership task's durable row before trusting any free-text message.
    configured = _configured_cwds(all_slots)
    cwd = _resolve(slot["cwd"])
    if configured.get(session_id) != cwd:
        raise ValueError("Recovery native CWD does not match the configured session roster")
    _validate_recovery_mapping(db, {"ownership_task_id": ownership_task_id,
                                    "owner_sid": session_id, "cwd": cwd}, configured)
    worktree = str(Path(owned_worktree).expanduser().resolve(strict=True))
    ownership_message = _task_message(db, ownership_task_id, session_id, cwd)
    if worktree not in ownership_message or branch not in ownership_message:
        raise ValueError("Owned worktree and branch are not recorded by this worker's owner task")
    task = db.execute("SELECT sid,cwd,state FROM tasks WHERE id=?", (failed_task_id,)).fetchone()
    if not task or task[0] != session_id or str(Path(task[1]).resolve()) != cwd:
        raise ValueError("Failed task ownership/cwd does not match the configured worker")
    if task[2] != "finished":
        raise ValueError("Failed task must remain terminal before recovery")
    if db.execute("SELECT 1 FROM tasks WHERE cwd=? AND id<>? AND state IN "
                  "('queued','dispatching','sent','running','uncertain') LIMIT 1",
                  (cwd, failed_task_id)).fetchone():
        raise ValueError("Another task owns this worktree; recovery is blocked")
    if db.execute("SELECT 1 FROM tasks WHERE id=?", (successor_task_id,)).fetchone():
        raise ValueError("Successor task ID already exists; choose a new task ID")

    values = {"id": recovery_id, "failed_sid": session_id, "failed_turn": turn,
              "failed_task_id": failed_task_id, "owner_sid": session_id,
              "owner_task_id": successor_task_id, "ownership_task_id": ownership_task_id,
              "cwd": cwd, "owned_worktree": worktree, "branch": branch,
              "head": head, "worktree_sha256": worktree_sha256, "note": note}

    # Hold both queue and session locks while establishing the recovery record.
    # Dispatch cannot see an authorized record until the verified old lock is released.
    session_lock = opencode._session_lock(opencode.session_dir(session_id))
    queue_lock = None
    try:
        queue_lock = _queue_lock()
        existing = db.execute("SELECT * FROM recoveries WHERE id=?", (recovery_id,)).fetchone()
        if existing:
            columns = [column[0] for column in
                       db.execute("SELECT * FROM recoveries LIMIT 0").description]
            row = dict(zip(columns, existing))
            if not _same_record(row, values):
                raise ValueError("Recovery ID was reused with different content")
            if row["status"] != "preparing":
                return {"recoveryId": recovery_id, "status": row["status"], "alreadyRecorded": True}
        else:
            if db.execute("SELECT 1 FROM recoveries WHERE failed_sid=? AND failed_turn=?",
                          (session_id, turn)).fetchone():
                raise ValueError("This failed OpenCode turn already has a recovery record")
            if _queue_has_task(successor_task_id):
                raise ValueError("Successor task ID already appears in the root queue")

        active_worktree_task = _active_task_for_worktree(db, worktree, configured, successor_task_id)
        if active_worktree_task:
            raise ValueError(f"Another active task owns the explicit worktree: {active_worktree_task}")
        queued_worktree_task = _queued_task_for_worktree(db, worktree, configured, successor_task_id)
        if queued_worktree_task:
            raise ValueError(f"Another queued task owns the explicit worktree: {queued_worktree_task}")

        verify_failed_turn(session_id, failed_task_id, turn, cwd, opencode.MODEL)

        snapshot = worktree_snapshot(worktree)
        if (snapshot["branch"] != branch or snapshot["head"] != head
                or snapshot["worktreeSha256"] != worktree_sha256):
            raise ValueError("Worktree branch/head/partial-state fingerprint changed")

        if not existing:
            now = time.time()
            db.execute("""INSERT INTO recoveries
                (id,failed_sid,failed_turn,failed_task_id,owner_sid,owner_task_id,
                 ownership_task_id,cwd,owned_worktree,branch,head,worktree_sha256,
                 note,status,created,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'preparing',?,?)""",
                       (recovery_id, session_id, turn, failed_task_id, session_id,
                        successor_task_id, ownership_task_id, cwd, worktree, branch, head,
                        worktree_sha256, note, now, now))
            db.execute("INSERT INTO events(kind,body,created) VALUES (?,?,?)",
                       ("opencode_recovery_prepared", json.dumps(values), now))
            db.commit()
    except sqlite3.IntegrityError as error:
        db.rollback()
        raise ValueError("An active recovery already owns this worker or worktree") from error
    except Exception:
        db.rollback()
        raise
    finally:
        if queue_lock:
            fcntl.flock(queue_lock.fileno(), fcntl.LOCK_UN)
            queue_lock.close()
        fcntl.flock(session_lock.fileno(), fcntl.LOCK_UN)
        session_lock.close()

    changed = db.execute("UPDATE recoveries SET status='authorized',updated=? "
                          "WHERE id=? AND status='preparing'",
                          (time.time(), recovery_id))
    db.commit()
    if changed.rowcount == 1:
        return {"recoveryId": recovery_id, "status": "authorized", "alreadyRecorded": False}
    row = db.execute("SELECT status FROM recoveries WHERE id=?", (recovery_id,)).fetchone()
    if row and row[0] == "authorized":
        return {"recoveryId": recovery_id, "status": row[0], "alreadyRecorded": True}
    raise ValueError("Recovery record could not be activated; replay the same request to resume")


def recovery_for_task(db, recovery_id, task_id, sid, previous_task_id,
                      statuses=("authorized",)):
    row = db.execute("SELECT * FROM recoveries WHERE id=?", (recovery_id,)).fetchone()
    if not row:
        return None
    names = [item[0] for item in db.execute("SELECT * FROM recoveries LIMIT 0").description]
    record = dict(zip(names, row))
    if (record["status"] not in statuses or record["owner_sid"] != sid
            or record["owner_task_id"] != task_id
            or record["failed_task_id"] != previous_task_id):
        return None
    return record


def claim_recovery(db, recovery_id, task_id, sid, previous_task_id):
    record = recovery_for_task(db, recovery_id, task_id, sid, previous_task_id)
    if not record:
        return None
    changed = db.execute("UPDATE recoveries SET status='claimed',updated=? "
                         "WHERE id=? AND status='authorized'",
                         (time.time(), recovery_id))
    if changed.rowcount != 1:
        return None
    record["status"] = "claimed"
    return record
