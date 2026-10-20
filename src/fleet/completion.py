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
completion receipt, and proof that the native execution stopped. Every unknown
is a refusal: an unreadable roster, an unreadable other-root ledger and a
missing process identity are never read as "nothing there". The session's
dispatch lock is held from the first validation through the ledger commit, so
a new native launch cannot start while this completion is being verified, and
the competing ownership is rechecked inside the transaction that closes the row.

It never rebuilds history, never replays a task, and never touches a hold, the
global pause, a dispatch flag, a model or an account. The original task body and
the retained turn receipt are preserved verbatim; the ledger row and one
auditable event are the only things it writes.

A completed turn ran a process, so its stopped proof is a full recorded native
identity and a recorded wrapper PID that are both provably gone; a receipt that
records no identity, or a null identity beside a successful completion, fails
closed. Ownership in a retired root is matched by task id, by session and by
canonical working directory, and that match is rechecked inside the transaction
that closes the row.
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
# Distinguishes "the key is absent" from "the key is present and null".
_MISSING = object()
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
    """Every session this fleet still manages, from one roster snapshot.

    The configuration is read once and any missing, invalid or unreadable
    roster is a refusal. An unknown roster must never be read as "this fleet
    no longer manages that worker", because that is the check that separates a
    stale row from a row this fleet still observes.
    """
    try:
        config = paths.config()
    except (OSError, ValueError) as error:
        raise ValueError("Fleet roster configuration is missing or unreadable") from error
    if not isinstance(config, dict):
        raise ValueError("Fleet roster configuration is invalid")
    try:
        pi_slots, claude_slots = paths.slots(config)
        opencode_roster = paths.opencode_slots(config)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError("Fleet roster configuration is invalid") from error
    sessions = set()
    for slot in list(pi_slots or []) + list(claude_slots or []) + list(opencode_roster or []):
        session_id = slot.get("sessionId") if isinstance(slot, dict) else None
        if isinstance(session_id, str) and session_id:
            sessions.add(session_id)
    return sessions


def _resolved_root(receipt_root):
    """The Fleet root that retains a turn receipt, resolved and verified."""
    if isinstance(receipt_root, Path):
        root = receipt_root
    elif isinstance(receipt_root, str) and receipt_root.strip():
        root = Path(receipt_root).expanduser()
    else:
        raise ValueError("Reconciliation requires the Fleet root that retains the turn receipt")
    if not root.is_absolute():
        raise ValueError("Receipt root must be an absolute path")
    if not root.is_dir():
        raise ValueError(f"Receipt root does not exist: {root}")
    return root.resolve()


def receipt_session_dir(receipt_root, session_id):
    """The managed session directory inside a Fleet root.

    The root may be a retired root: a fleet replacement keeps the completed
    turn's runtime in place while the ledger moves to the new root. The path is
    built from the resolved root so a symlinked root still compares
    canonically, and a symlinked segment is refused rather than followed.
    """
    root = _resolved_root(receipt_root)
    directory = root / "data" / "opencode" / session_id
    if not directory.is_dir():
        raise ValueError("Retained native session directory is missing for this worker")
    if directory.resolve() != directory:
        raise ValueError("Retained native session path is not canonical")
    return directory


def receipt_turn_dir(session_directory, turn):
    """The retained managed turn directory of one session."""
    directory = Path(session_directory) / "turns" / turn
    if not directory.is_dir():
        raise ValueError("Retained native turn receipt is missing for this worker and turn")
    if directory.resolve() != directory:
        raise ValueError("Retained native turn receipt path is not canonical")
    return directory


def _hold_session_lock(session_directory):
    """Take the session's dispatch lock and keep it until the commit ends.

    A managed launch takes the same lock, so holding it closes the window in
    which a new turn could start while this completion is verified. A lock that
    is already held means a managed turn is active for this session.
    """
    try:
        return opencode._session_lock(Path(session_directory))
    except RuntimeError as error:
        raise ValueError("A managed turn is active for this session") from error
    except OSError as error:
        raise ValueError("The session dispatch lock could not be taken") from error


def _release_session_lock(handle):
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _other_roots_claim(db_path, task_id, session_id, resolved_cwd):
    """The active owner of this work in another Fleet root, if any.

    A retired root keeps its own ledger. Only a database that was read
    successfully and holds no active owner proves that the root does not claim
    the work. A missing, unreadable or corrupt ledger is unknown, not absent,
    and refuses the reconciliation.

    Ownership is matched by task id, by session and by canonical working
    directory: a different task in that root that runs under the same session
    or the same working directory owns the same work and blocks the
    reconciliation just as the same task id would.
    """
    root = Path(db_path).expanduser().resolve()
    if root == Path(paths.DB).expanduser().resolve():
        return None  # The same ledger; it is the one being reconciled.
    if not root.is_file():
        raise ValueError("Retired root ledger is missing; ownership cannot be verified")
    uri = "file:" + str(root) + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2) as other:
            other.execute("PRAGMA query_only=ON")
            rows = other.execute("SELECT id,sid,cwd,state FROM tasks WHERE state IN "
                                 "('dispatching','sent','running','uncertain')").fetchall()
    except sqlite3.Error as error:
        raise ValueError("Retired root ledger is unreadable; ownership cannot be verified") from error
    for other_id, other_sid, other_cwd, other_state in rows:
        if other_id == task_id or other_sid == session_id \
                or (resolved_cwd and _resolve(other_cwd) == resolved_cwd):
            return {"id": other_id, "sid": other_sid, "state": other_state}
    return None


def _assert_stopped_native(result, state_document, pointer):
    """Prove that the completed turn's native execution stopped.

    A completed turn ran a process: the wrapper cannot finish as ``done``
    without a spawned native process and a verified identity, and the
    no-process paths end ``blocked`` rather than completed. A receipt that
    records no identity, or records a null one beside a successful
    completion, contradicts that and fails closed. A full recorded identity
    and a recorded wrapper PID are both required, and both must be gone.
    """
    identity = result.get("managedProcessIdentity", _MISSING)
    if identity is _MISSING:
        identity = state_document.get("processIdentity", _MISSING)
    if identity is _MISSING:
        raise ValueError("Native receipt records no process identity; the stopped proof is missing")
    if identity is None:
        raise ValueError("Native receipt records no native process identity "
                         "for a completed turn")
    if not isinstance(identity, dict):
        raise ValueError("Native receipt has an invalid native process identity")
    for key in ("pid", "pgid"):
        value = identity.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("Native receipt has an invalid native process identity")
    start = identity.get("start")
    if not isinstance(start, (list, tuple)) or not start \
            or not all(isinstance(item, int) and not isinstance(item, bool) for item in start):
        raise ValueError("Native receipt has an invalid native process identity")
    executable = identity.get("executable")
    if not isinstance(executable, str) or not executable.strip():
        raise ValueError("Native receipt has an invalid native process identity")
    if opencode.identity_matches(identity):
        raise ValueError("Native managed process is still live")
    if opencode._group_exists(identity["pgid"]):
        raise ValueError("Native managed process group is still present")
    wrapper_pid = pointer.get("wrapperPid", _MISSING)
    if wrapper_pid is _MISSING:
        raise ValueError("Recorded wrapper PID is missing; the stopped proof is incomplete")
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
    return "gone"


def _conflict_rows(db, task_id):
    """Every other task that still owns this worker or its working directory.

    The rows are read twice: once while the receipt is verified and once
    inside the transaction that closes the row, so a task that claims the
    working directory between the two is refused instead of raced.
    """
    return db.execute("SELECT id,cwd,sid FROM tasks WHERE id<>? AND state IN "
                      "('dispatching','sent','running','uncertain')", (task_id,)).fetchall()


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


def _assert_no_conflict(rows, task_id, session_id, resolved_cwd):
    """Refuse a competing owner of the worker or of the working directory."""
    for other_id, other_cwd, other_sid in rows:
        if other_sid == session_id:
            raise ValueError(f"Worker has another non-terminal task: {other_id}")
        if _resolve(other_cwd) == resolved_cwd:
            raise ValueError(f"Another task owns this working directory: {other_id}")


def reconcile_completed(db, task_id, turn, session_id, receipt_root, note):
    """Close one stale running row whose managed OpenCode turn provably completed.

    Returns the recorded reconciliation. Every refusal raises ``ValueError``
    with the ledger unchanged, so a task is never flipped on an unverified
    receipt, a live process, an unknown roster or an uncertain owner.
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
    # One roster snapshot, and an unreadable roster refuses instead of
    # pretending that this fleet no longer manages the worker.
    roster = _managed_sessions()
    if session_id in roster:
        raise ValueError("Worker is still managed by this fleet; its row is observed normally")

    resolved_cwd = _resolve(cwd)
    if not resolved_cwd:
        raise ValueError("Task has no working directory to reconcile")
    _assert_no_conflict(_conflict_rows(db, task_id), task_id, session_id, resolved_cwd)

    root = _resolved_root(receipt_root)
    session_directory = receipt_session_dir(root, session_id)
    # From here the session's dispatch lock is held, so no managed launch can
    # start for this session while the completion is verified and committed.
    session_lock = _hold_session_lock(session_directory)
    try:
        directory = receipt_turn_dir(session_directory, turn)
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
        for label, document in (("request", request), ("result", result),
                                ("turn pointer", pointer)):
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
        if not _is_number(state_document.get("finishedAt")) \
                or not _is_number(result.get("finishedAt")):
            raise ValueError("Native receipt has no completion timestamp")
        if not isinstance(result.get("terminalReason"), str) \
                or not result["terminalReason"].strip():
            raise ValueError("Native receipt has no terminal reason")

        # The stopped proof is required, not optional: a completed turn ran a
        # process, and both the recorded native identity and the recorded
        # wrapper PID have to be provably gone.
        process_proof = _assert_stopped_native(result, state_document, pointer)

        claimed = _other_roots_claim(root / "data" / "fleet.sqlite", task_id, session_id,
                                     resolved_cwd)
        if claimed:
            raise ValueError("Another Fleet root still owns the task")

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
            "processIdentity": process_proof,
            "wrapperPid": pointer.get("wrapperPid"),
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
            # Recheck the roster, the competing ownership and the retired root
            # inside the transaction, so a worker that rejoined the roster, a
            # task that claimed the working directory, or a claim that appeared
            # in the retired root between the checks is refused instead of
            # raced.
            if session_id in _managed_sessions():
                db.rollback()
                raise ValueError("Worker is still managed by this fleet; "
                                 "its row is observed normally")
            _assert_no_conflict(_conflict_rows(db, task_id), task_id, session_id, resolved_cwd)
            claimed = _other_roots_claim(root / "data" / "fleet.sqlite", task_id, session_id,
                                         resolved_cwd)
            if claimed:
                db.rollback()
                raise ValueError("Another Fleet root still owns the task")
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
    finally:
        _release_session_lock(session_lock)
