"""Guarded reconciliation of a stale running row whose managed OpenCode turn completed.

A managed turn can finish natively while its Fleet ledger row stays ``running``.
The manager closes such a row while it observes the worker, so the row survives
only when the worker left the roster (a fleet replacement) or the manager was not
running when the turn ended. The surviving row keeps the same-cwd dispatch
exclusivity forever: ``resolve`` only accepts ``uncertain`` rows and ``cancel``
aborts a live turn, so no supported control can move a row whose turn already
finished. This module adds exactly one narrow operation for that case.

The operation is a reconciliation, not a release. It requires the exact task,
turn, worker, session and working-directory identity, the retained native
completion receipt, and proof that the native execution stopped. It refuses a
missing, uncertain or inconsistent receipt, a live process identity, an active
managed turn, a worker this fleet still manages, and any competing task for the
same working directory. It never rebuilds history, never replays a task, and
never touches a hold, the global pause, a dispatch flag, a model or an account.
The original task body and the retained turn receipt are preserved verbatim; the
ledger row and one auditable event are the only things it writes.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from . import opencode, paths
from .store import emit

# Native terminal states that prove a successful completion. "stopped" is an
# abort, and "error"/"blocked" are failures; neither is a completion.
_COMPLETED_NATIVE = {"done", "completed"}
# Ledger states a completed-but-unobserved row can still carry.
_STALE_STATES = {"running", "sent"}
# Ledger states that own a worker or a working directory.
_OWNING_STATES = ("dispatching", "sent", "running", "uncertain")
_EVENT_KIND = "opencode_completed_reconciled"


def _resolve(value):
    """Resolve a path for identity comparison; empty values stay empty."""
    return str(Path(value).expanduser().resolve()) if value else ""


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path, label):
    """Read one receipt document, refusing a missing or unreadable file."""
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise ValueError(f"Native {label} is missing; reconciliation is blocked") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Native {label} is unreadable; reconciliation is blocked") from error
    if not isinstance(value, dict):
        raise ValueError(f"Native {label} is not an object; reconciliation is blocked")
    return value


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _managed_sessions():
    """Every session this fleet still manages, from the configured roster."""
    try:
        pi_slots, claude_slots = paths.slots()
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        pi_slots, claude_slots = [], []
    try:
        opencode_roster = paths.opencode_slots()
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        opencode_roster = []
    sessions = set()
    for slot in list(pi_slots or []) + list(claude_slots or []) + list(opencode_roster or []):
        session_id = slot.get("sessionId") if isinstance(slot, dict) else None
        if isinstance(session_id, str) and session_id:
            sessions.add(session_id)
    return sessions


def receipt_dir(receipt_root, session_id, turn):
    """The retained managed turn directory inside a Fleet root.

    The root may be a retired root: a fleet replacement keeps the completed
    turn's runtime in place while the ledger moves to the new root. The path is
    built from the resolved root so a symlinked root still compares canonically,
    and a symlinked turn segment is refused rather than followed.
    """
    if not isinstance(receipt_root, str) or not receipt_root.strip():
        raise ValueError("Reconciliation requires the Fleet root that retains the turn receipt")
    root = Path(receipt_root).expanduser()
    if not root.is_absolute():
        raise ValueError("Receipt root must be an absolute path")
    if not root.is_dir():
        raise ValueError(f"Receipt root does not exist: {root}")
    root = root.resolve()
    directory = root / "data" / "opencode" / session_id / "turns" / turn
    if not directory.is_dir():
        raise ValueError("Retained native turn receipt is missing for this worker and turn")
    if directory.resolve() != directory:
        raise ValueError("Retained native turn receipt path is not canonical")
    return directory


def _session_lock_free(directory):
    """True when no managed launch holds this session's dispatch lock.

    The lock file is only inspected, never created: a retired root keeps the
    evidence it already has.
    """
    lock = Path(directory) / "dispatch.lock"
    if not lock.exists():
        return True
    try:
        handle = lock.open("rb")
    except OSError:
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True
    finally:
        handle.close()


def _other_roots_claim(db_path, task_id):
    """Does another Fleet root still own this task in a non-terminal state?

    The receipt root keeps its own ledger. When that ledger is a different
    database and still carries the task, two roots claim the same work and the
    reconciliation is refused.
    """
    root = Path(db_path).expanduser().resolve()
    if root == Path(paths.DB).expanduser().resolve():
        return None
    if not root.is_file():
        return None
    uri = "file:" + str(root) + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2) as other:
            other.execute("PRAGMA query_only=ON")
            row = other.execute("SELECT sid,state FROM tasks WHERE id=?", (task_id,)).fetchone()
    except sqlite3.Error:
        return None
    if row and row[1] in _OWNING_STATES:
        return {"sid": row[0], "state": row[1]}
    return None


def _existing_record(db, task_id, turn):
    """The recorded reconciliation for this task and turn, if there is one."""
    for event_id, body in db.execute("SELECT id,body FROM events WHERE kind=?",
                                     (_EVENT_KIND,)).fetchall():
        try:
            evidence = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            continue
        if evidence.get("taskId") == task_id and evidence.get("turn") == turn:
            return {"id": event_id, "evidence": evidence}
    return None


def reconcile_completed(db, task_id, turn, session_id, receipt_root, note):
    """Close one stale running row whose managed OpenCode turn provably completed.

    Returns the recorded reconciliation. Every refusal raises ``ValueError``
    with the ledger unchanged, so a task is never flipped on an unverified
    receipt, a live process, or an uncertain owner.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("Reconciliation requires the exact task id")
    if not isinstance(turn, str) or not opencode._TURN_ID.fullmatch(turn):
        raise ValueError("Reconciliation turn is invalid")
    if not isinstance(session_id, str) or not opencode._SESSION_ID.fullmatch(session_id):
        raise ValueError("Reconciliation worker session id is invalid")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Reconciliation requires an operator note")

    row = db.execute("SELECT sid,cwd,state FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise ValueError("Task not found")
    sid, cwd, state = row
    if sid != session_id:
        raise ValueError("Task does not belong to the requested worker session")
    if state == "uncertain":
        raise ValueError("Task is uncertain; resolve it with `fleet resolve`, not this operation")
    if session_id in _managed_sessions():
        raise ValueError("Worker is still managed by this fleet; its row is observed normally")

    resolved_cwd = _resolve(cwd)
    if not resolved_cwd:
        raise ValueError("Task has no working directory to reconcile")
    for other_id, other_cwd, other_sid in db.execute(
            "SELECT id,cwd,sid FROM tasks WHERE id<>? AND state IN "
            "('dispatching','sent','running','uncertain')", (task_id,)).fetchall():
        if other_sid == session_id:
            raise ValueError(f"Worker has another non-terminal task: {other_id}")
        if _resolve(other_cwd) == resolved_cwd:
            raise ValueError(f"Another task owns this working directory: {other_id}")

    directory = receipt_dir(receipt_root, session_id, turn)
    session_directory = directory.parent.parent
    request = _read_json(directory / "request.json", "request")
    state_document = _read_json(directory / "state.json", "state")
    result = _read_json(directory / "result.json", "result")
    pointer_path = session_directory / "current.json"
    pointer = _read_json(pointer_path, "turn pointer")

    identity = {"taskId": task_id, "sessionId": session_id}
    for label, document in (("request", request), ("state", state_document),
                            ("result", result), ("turn pointer", pointer)):
        for key, expected in identity.items():
            if document.get(key) != expected:
                raise ValueError(
                    f"Native {label} does not identify the requested task and session")
        if _resolve(document.get("cwd")) != resolved_cwd:
            raise ValueError(f"Native {label} working directory differs from the task row")
    # The request, the result and the session pointer record the turn id. The
    # state document does not: it lives in the turn's own directory, which is
    # what binds it to the same turn.
    for label, document in (("request", request), ("result", result), ("turn pointer", pointer)):
        if document.get("turn") != turn:
            raise ValueError(f"Native {label} does not identify the requested turn")
    if "turn" in state_document and state_document.get("turn") != turn:
        raise ValueError("Native state does not identify the requested turn")
    if request.get("model") != state_document.get("model"):
        raise ValueError("Native request and state disagree about the model")

    if state_document.get("state") not in _COMPLETED_NATIVE \
            or result.get("state") not in _COMPLETED_NATIVE:
        raise ValueError("Native receipt does not prove a completed turn")
    if state_document.get("exitCode") != 0 or result.get("exitCode") != 0:
        raise ValueError("Native receipt does not prove a successful exit")
    if state_document.get("cleanup") != "confirmed" or result.get("cleanup") != "confirmed":
        raise ValueError("Native receipt does not confirm cleanup")
    if not _is_number(state_document.get("finishedAt")) or not _is_number(result.get("finishedAt")):
        raise ValueError("Native receipt has no completion timestamp")
    if not isinstance(result.get("terminalReason"), str) or not result["terminalReason"].strip():
        raise ValueError("Native receipt has no terminal reason")

    process_identity = result.get("managedProcessIdentity") or state_document.get("processIdentity")
    if process_identity is not None:
        if not isinstance(process_identity, dict) or not process_identity.get("pid") \
                or not process_identity.get("pgid"):
            raise ValueError("Native receipt has an invalid native process identity")
        if opencode.identity_matches(process_identity):
            raise ValueError("Native managed process is still live")
        if opencode._group_exists(process_identity["pgid"]):
            raise ValueError("Native managed process group is still present")
    wrapper_pid = pointer.get("wrapperPid")
    if wrapper_pid is not None:
        if not isinstance(wrapper_pid, int) or isinstance(wrapper_pid, bool) or wrapper_pid <= 0:
            raise ValueError("Recorded wrapper PID is invalid")
        try:
            os.kill(wrapper_pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise ValueError("Recorded wrapper PID cannot be verified") from error
        else:
            raise ValueError("Recorded wrapper PID is still present")
    if not _session_lock_free(session_directory):
        raise ValueError("A managed turn is active for this session")

    root = Path(receipt_root).expanduser().resolve()
    claimed = _other_roots_claim(root / "data" / "fleet.sqlite", task_id)
    if claimed:
        raise ValueError("Another Fleet root still owns this task")

    proof = {
        "taskId": task_id,
        "turn": turn,
        "sessionId": session_id,
        "cwd": resolved_cwd,
        "recordedCwd": cwd,
        "exitCode": 0,
        "terminalReason": result.get("terminalReason"),
        "cleanup": "confirmed",
        "finishedAt": result.get("finishedAt"),
        "receiptRoot": str(root),
        "receiptDir": str(directory),
        "requestSha256": _digest(directory / "request.json"),
        "stateSha256": _digest(directory / "state.json"),
        "resultSha256": _digest(directory / "result.json"),
        "pointerSha256": _digest(pointer_path),
        "processIdentity": "gone" if process_identity is not None else "not-recorded",
        "wrapperPid": wrapper_pid,
    }
    evidence = dict(proof, previousState=state, state="finished", note=note.strip())

    existing = _existing_record(db, task_id, turn)
    if existing:
        recorded = existing["evidence"]
        if any(recorded.get(key) != value for key, value in proof.items()):
            raise ValueError("Previously audited reconciliation evidence changed")
        return {"taskId": task_id, "turn": turn, "sessionId": session_id, "reconciled": True,
                "alreadyRecorded": True, "state": "finished", "previousState": state,
                "record": existing["id"], "evidence": recorded}
    if state not in _STALE_STATES:
        raise ValueError(f"Task is {state}; only a stale running or sent row can be reconciled")

    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        current = db.execute("SELECT sid,cwd,state FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not current or current[0] != sid or current[1] != cwd or current[2] != state:
            db.rollback()
            raise ValueError("Task changed during validation; inspect before reconciling")
        changed = db.execute("UPDATE tasks SET state='finished',updated=? WHERE id=? "
                             "AND state IN ('running','sent')", (time.time(), task_id))
        if changed.rowcount != 1:
            db.rollback()
            raise ValueError("Task state changed; inspect before reconciling")
        record = emit(db, _EVENT_KIND, **evidence)
    except Exception:
        db.rollback()
        raise
    return {"taskId": task_id, "turn": turn, "sessionId": session_id, "reconciled": True,
            "alreadyRecorded": False, "state": "finished", "previousState": state,
            "record": record, "evidence": evidence}
