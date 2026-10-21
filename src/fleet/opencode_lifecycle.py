"""On-demand OpenCode session lifecycle: idempotent add, drain and safe removal.

A native session is created only through the supported OpenCode CLI API
(``session.create`` on ``POST /api/session``), which is the same supported path
an operator's own CLI uses. The Fleet never writes OpenCode's internal database
and never invents a session id: the id in a receipt is the id the native server
returned, and the roster is written only after that id was read back from the
native identity row.

Creating a session does not contact a provider: the request carries the pinned
model and the directory, and the native server records them. No model call, no
paid resource and no credential access is involved in this path.

The roster is the only source of a worker's identity, so every addition is
recorded in a durable receipt, every retry of the same request key is idempotent,
and a partial failure leaves the roster untouched while the native sessions that
were created are kept and reported. Removal never deletes a native session and
never touches a hold, a task, a queue entry or a worktree.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from . import heavy, opencode, ops, paths
from .store import emit

# One request never creates more than this many sessions. The bound keeps a
# single command from becoming an unbounded fleet growth, and it is far below
# the point where the admission limit or the heavy-job scheduler matter.
MAX_ADD_COUNT = 16
# The validated ceiling for the fleet admission limit. A request may raise the
# limit only inside this ceiling; the limit is never raised implicitly.
MAX_CONCURRENT_CAP = 64
# A native creation is bounded so a hung CLI cannot hold the operator's shell.
CREATE_TIMEOUT_SECONDS = 120
# A task in any of these states is outstanding for a session: it is work that
# must not be dropped by a drain or a removal.
OUTSTANDING_STATES = ("queued", "dispatching", "sent", "running", "uncertain")
# A recovery in one of these states holds a durable ownership claim on a
# session; removing that session would strand the claim.
ACTIVE_RECOVERY_STATES = ("preparing", "authorized", "claimed")
_SESSION_ID = re.compile(r"ses_[A-Za-z0-9_-]+\Z")
_REQUEST_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


class ProvisioningError(RuntimeError):
    """A native session could not be created; the roster was not changed."""


class ProvisioningUncertain(ProvisioningError):
    """The outcome of a native creation is unknown; the roster was not changed."""


# --- validation ------------------------------------------------------------

def _resolve(path_value):
    return Path(os.path.expanduser(str(path_value))).resolve()


def _account_of(entries, requested=None):
    """The one native account the roster already uses.

    The account is the native OpenCode database the configured workers share. A
    request for a different database is refused: a session created in another
    account is not the account this fleet manages, and the fleet never creates
    an account.
    """
    accounts = {entry.get("databasePath") for entry in entries
                if isinstance(entry, dict) and entry.get("databasePath")}
    if requested:
        account = str(_resolve(requested))
        if accounts and account not in {str(_resolve(a)) for a in accounts}:
            raise ValueError("The requested databasePath is not the native account this "
                             "roster uses; a session is never created in another account")
        return account
    if not accounts:
        raise ValueError("No existing OpenCode account to extend; the roster names no "
                         "databasePath for an OpenCode worker")
    if len(accounts) > 1:
        raise ValueError("The roster spans more than one native OpenCode account; the "
                         "account to extend is ambiguous")
    return str(_resolve(next(iter(accounts))))


def validate_request(cfg, count, model, cwd, root, database_path=None):
    """Validate the whole request before anything is created or written.

    Every check fails closed: a wrong root, a wrong account, a model that is not
    the account's approved model, a working directory outside the worktree root
    or a malformed count refuses the request instead of adjusting it.
    """
    expected_root = _resolve(paths.ROOT)
    if _resolve(root) != expected_root:
        raise ValueError(f"This command must run for the fleet root {expected_root}; "
                         f"{_resolve(root)} is a different root")
    entries = [entry for entry in cfg.get("opencode", []) if isinstance(entry, dict)]
    account = _account_of(entries, database_path)
    provider, name = opencode._validate_model(model)
    roster_models = {entry.get("model") for entry in entries if entry.get("model")}
    if roster_models and model not in roster_models:
        raise ValueError("The requested model differs from the approved model this account's "
                         "workers are configured with: " + ", ".join(sorted(roster_models)))
    if type(count) is not int or count < 1:
        raise ValueError("The number of sessions must be a positive whole number")
    if count > MAX_ADD_COUNT:
        raise ValueError(f"The number of sessions must not exceed {MAX_ADD_COUNT}")
    workdir = ops._check_cwd(cfg, cwd)
    return {"root": str(expected_root), "account": account, "model": model,
            "provider": provider, "modelName": name, "cwd": workdir, "count": count}


def validate_max_concurrent(current, requested, logical_sessions):
    """Validate an explicitly requested admission limit.

    The limit is never changed implicitly. A request may only raise it, only
    inside the validated ceiling, and only to a value the logical session count
    can carry. The heavy-job admission is a separate, unchanged bound.
    """
    if requested is None:
        return None
    if type(requested) is not int or requested < 0:
        raise ValueError("maxConcurrent must be a whole number of tasks in flight")
    if requested < current:
        raise ValueError(f"maxConcurrent is {current}; this command never lowers it")
    if requested > MAX_CONCURRENT_CAP:
        raise ValueError(f"maxConcurrent must not exceed {MAX_CONCURRENT_CAP}")
    if requested > max(current, 2 * logical_sessions):
        raise ValueError("maxConcurrent must not exceed twice the logical session count "
                         f"({logical_sessions})")
    return requested


def derive_request_key(root, account, model, cwd, count, enable_dispatch):
    """A deterministic key for the same request, so a retry is idempotent."""
    material = json.dumps([root, account, model, cwd, count, bool(enable_dispatch)],
                          sort_keys=True, separators=(",", ":"))
    return "opencode-" + hashlib.sha256(material.encode()).hexdigest()[:32]


def validate_request_key(value):
    if value is None:
        return None
    if not isinstance(value, str) or not _REQUEST_KEY.fullmatch(value):
        raise ValueError("The request key must be 1-128 characters of A-Z a-z 0-9 . _ : -")
    return value


# --- native session creation ----------------------------------------------

def _parse_session(output):
    """Read the session the native server returned; never invent one."""
    text = (output or "").strip()
    if not text:
        raise ProvisioningError("The OpenCode CLI returned no session")
    candidates = [text]
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            candidates.append(line)
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        session = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        identifier = session.get("id")
        if isinstance(identifier, str) and _SESSION_ID.fullmatch(identifier):
            return session
    raise ProvisioningError("The OpenCode CLI did not return a native session id")


def _terminate(process):
    """Stop a hung native creation; the outcome stays unknown, never assumed."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except (ProcessLookupError, OSError):
            return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def create_native_session(executable, account, cwd, model, scratch,
                          timeout=CREATE_TIMEOUT_SECONDS):
    """Create one native session through the supported OpenCode CLI API.

    The isolated runtime pins the model and its provider and keeps the native
    account database as the only durable state, exactly like a managed turn. The
    session id is the one the native server returned; a failure is reported and
    never turned into a fabricated id.
    """
    request = {"model": model, "turnRoot": str(scratch), "executable": str(executable),
               "databasePath": str(account)}
    env = opencode._runtime_environment(request)
    payload = {"location": {"directory": str(cwd)}, "model": model}
    command = [str(executable), "api", "--standalone", "POST", "/api/session",
               "--data", json.dumps(payload, separators=(",", ":"))]
    try:
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
    except OSError as error:
        raise ProvisioningError("The OpenCode CLI could not be started: "
                                + error.__class__.__name__) from error
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate(process)
        raise ProvisioningUncertain(
            "The native session creation did not answer in time; whether the session "
            "exists is unknown, so no roster entry was written") from None
    if process.returncode != 0:
        raise ProvisioningError("The OpenCode CLI could not create a native session: "
                                + (stderr or "").strip()[:400])
    return _parse_session(stdout)


def _verify_created(session_id, cwd, account):
    """Read the created session's own identity row back, read-only."""
    return opencode.verify_native_session(session_id, cwd, account)


# --- receipts ---------------------------------------------------------------

def _provisioning_row(db, request_key):
    return db.execute("SELECT request_key,model,account,cwd,root,requested_count,"
                      "dispatch_enabled,status,created,updated,detail "
                      "FROM opencode_provisioning WHERE request_key=?",
                      (request_key,)).fetchone()


def _session_receipts(db, request_key=None):
    if request_key is None:
        rows = db.execute("SELECT session_id,request_key,native_id,model,account,cwd,root,"
                          "status,created,updated,detail FROM opencode_sessions "
                          "ORDER BY created").fetchall()
    else:
        rows = db.execute("SELECT session_id,request_key,native_id,model,account,cwd,root,"
                          "status,created,updated,detail FROM opencode_sessions "
                          "WHERE request_key=? ORDER BY created", (request_key,)).fetchall()
    columns = ("session_id", "request_key", "native_id", "model", "account", "cwd", "root",
               "status", "created", "updated", "detail")
    return [dict(zip(columns, row)) for row in rows]


def _record_provisioning(db, request_key, request, status, detail="", count=None):
    now = time.time()
    db.execute(
        "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
        "requested_count,dispatch_enabled,status,created,updated,detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(request_key) DO UPDATE SET status=excluded.status,"
        "updated=excluded.updated,detail=excluded.detail",
        (request_key, request["model"], request["account"], request["cwd"], request["root"],
         request["count"] if count is None else count, int(request["dispatchEnabled"]),
         status, now, now, detail))
    db.commit()


def _record_session(db, session_id, request_key, request, status, detail=""):
    now = time.time()
    db.execute(
        "INSERT INTO opencode_sessions (session_id,request_key,native_id,model,account,cwd,"
        "root,status,created,updated,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(session_id) DO UPDATE SET status=excluded.status,"
        "updated=excluded.updated,detail=excluded.detail",
        (session_id, request_key, session_id, request["model"], request["account"],
         request["cwd"], request["root"], status, now, now, detail))
    db.commit()


# --- reporting --------------------------------------------------------------

def status(db, root=None):
    """Report the account, the roster and the provisioning receipts. Read-only."""
    cfg = paths.config()
    entries = [entry for entry in cfg.get("opencode", []) if isinstance(entry, dict)]
    accounts = sorted({str(entry.get("databasePath")) for entry in entries
                       if entry.get("databasePath")})
    receipts = _session_receipts(db)
    registered = [receipt for receipt in receipts if receipt["status"] == "registered"]
    logical = len(entries)
    return {
        "action": "add-opencode-status",
        "root": str(_resolve(paths.ROOT)),
        "config": str(paths.CONFIG),
        "accounts": accounts,
        "logicalSessions": logical,
        "admissionLimit": int(cfg.get("maxConcurrent", 0)),
        "heavyJobSlots": heavy.SLOT_COUNT,
        "managerRunning": bool(ops._manager_locked()),
        "roster": [{"sessionId": entry.get("sessionId"), "model": entry.get("model"),
                    "cwd": entry.get("cwd"), "account": entry.get("databasePath"),
                    "dispatchEnabled": bool(entry.get("dispatchEnabled", False)),
                    "draining": bool(entry.get("draining", False))} for entry in entries],
        "receipts": receipts,
        "registeredReceipts": len(registered),
    }


# --- add --------------------------------------------------------------------

def add_sessions(db, count, model, cwd, root, database_path=None, request_key=None,
                 enable_dispatch=False, max_concurrent=None, dry_run=False, note=""):
    """Add OpenCode sessions to the roster, idempotently.

    The request is validated in full before anything is created. The roster is
    written once, after every session was created and read back from the native
    identity row, so a partial failure leaves the roster exactly as it was. The
    native sessions that were created before a failure are kept and recorded:
    they are never deleted, and a retry of the same request key registers them
    instead of creating a second set.
    """
    cfg = paths.config()
    request = validate_request(cfg, count, model, cwd, root, database_path)
    request["dispatchEnabled"] = bool(enable_dispatch)
    key = validate_request_key(request_key) or derive_request_key(
        request["root"], request["account"], request["model"], request["cwd"],
        count, enable_dispatch)
    entries = [entry for entry in cfg.get("opencode", []) if isinstance(entry, dict)]
    existing = _provisioning_row(db, key)
    if existing is not None and existing[7] == "registered":
        receipts = [receipt for receipt in _session_receipts(db, key)
                    if receipt["status"] == "registered"]
        return {"action": "add-opencode", "idempotent": True, "requestKey": key,
                "created": 0, "registered": len(receipts), "requested": count,
                "sessions": [{"sessionId": receipt["session_id"],
                              "dispatchEnabled": bool(existing[6])} for receipt in receipts],
                "model": request["model"], "account": request["account"],
                "cwd": request["cwd"], "root": request["root"],
                "logicalSessions": len(entries), "admissionLimit": int(cfg.get("maxConcurrent", 0)),
                "heavyJobSlots": heavy.SLOT_COUNT, "dryRun": False,
                "note": existing[10]}
    pending = [receipt for receipt in _session_receipts(db, key)
               if receipt["status"] == "created"]
    registered = [receipt for receipt in _session_receipts(db, key)
                  if receipt["status"] == "registered"]
    needed = count - len(pending) - len(registered)
    if needed < 0:
        needed = 0
    plan = {"action": "add-opencode", "idempotent": False, "requestKey": key,
            "requested": count, "toCreate": needed,
            "created": 0, "registered": len(registered),
            "alreadyRegistered": len(registered), "pendingNative": len(pending),
            "model": request["model"], "account": request["account"],
            "cwd": request["cwd"], "root": request["root"],
            "dispatchEnabled": request["dispatchEnabled"],
            "logicalSessions": len(entries) + needed,
            "admissionLimit": int(cfg.get("maxConcurrent", 0)),
            "heavyJobSlots": heavy.SLOT_COUNT,
            "maxConcurrent": {"before": int(cfg.get("maxConcurrent", 0)),
                              "after": int(cfg.get("maxConcurrent", 0))},
            "dryRun": bool(dry_run)}
    limit = validate_max_concurrent(int(cfg.get("maxConcurrent", 0)), max_concurrent,
                                    plan["logicalSessions"])
    if limit is not None:
        plan["maxConcurrent"]["after"] = limit
    if dry_run:
        return plan
    if needed == 0 and not pending:
        _record_provisioning(db, key, request, "registered", "nothing to add")
        return {**plan, "created": 0, "registered": len(registered), "sessions": []}
    executable = paths.executable("opencode", cfg)
    scratch = paths.DATA / "opencode" / "provision" / key
    scratch.mkdir(parents=True, exist_ok=True)
    _record_provisioning(db, key, request, "provisioning", "creating native sessions")
    created = []
    try:
        for _ in range(needed):
            session = create_native_session(executable, request["account"], request["cwd"],
                                            request["model"], scratch)
            identifier = session["id"]
            try:
                _verify_created(identifier, request["cwd"], request["account"])
            except (ValueError, OSError) as error:
                _record_session(db, identifier, key, request, "unverified", str(error))
                raise ProvisioningError(
                    "The created native session could not be read back: " + str(error)) from error
            _record_session(db, identifier, key, request, "created", "native session created")
            created.append(identifier)
    except ProvisioningError as error:
        _record_provisioning(db, key, request, "failed", str(error))
        emit(db, "needs_attention", category="opencode-provisioning", requestKey=key,
             error=str(error), created=created)
        return {**plan, "created": len(created), "registered": 0,
                "createdNotRegistered": created, "sessions": [],
                "logicalSessions": len(entries),
                "failed": str(error), "rosterChanged": False}
    updated = paths.config()
    for identifier in [receipt["session_id"] for receipt in pending] + created:
        if any(entry.get("sessionId") == identifier for entry in updated.get("opencode", [])):
            continue
        updated.setdefault("opencode", []).append({
            "sessionId": identifier, "cwd": request["cwd"], "sessionCwd": request["cwd"],
            "databasePath": request["account"], "model": request["model"],
            "provider": "opencode", "dispatchEnabled": request["dispatchEnabled"]})
    if limit is not None:
        updated["maxConcurrent"] = limit
    paths.write_config(updated)
    for identifier in [receipt["session_id"] for receipt in pending] + created:
        _record_session(db, identifier, key, request, "registered", "in the roster")
    _record_provisioning(db, key, request, "registered", "roster updated")
    emit(db, "opencode_sessions_added", requestKey=key, sessions=created,
         model=request["model"], account=request["account"], note=note)
    return {**plan, "created": len(created),
            "registered": len([receipt["session_id"] for receipt in pending]) + len(created),
            "sessions": [{"sessionId": identifier,
                          "dispatchEnabled": request["dispatchEnabled"]}
                         for identifier in ([receipt["session_id"] for receipt in pending]
                                             + created)],
            "rosterChanged": True}


# --- drain and removal -----------------------------------------------------

def _roster_entry(cfg, session_id):
    matches = [entry for entry in cfg.get("opencode", [])
               if isinstance(entry, dict) and entry.get("sessionId") == session_id]
    if len(matches) != 1:
        raise ValueError("The session is not one configured OpenCode worker")
    return matches[0]


def _outstanding_tasks(db, session_id):
    rows = db.execute("SELECT id,state FROM tasks WHERE sid=? AND state IN "
                      f"({','.join('?' * len(OUTSTANDING_STATES))}) ORDER BY id",
                      (session_id, *OUTSTANDING_STATES)).fetchall()
    return [{"taskId": row[0], "state": row[1]} for row in rows]


def _active_recovery(db, session_id):
    rows = db.execute("SELECT id,status,owner_sid,failed_sid FROM recoveries WHERE "
                      f"(owner_sid=? OR failed_sid=?) AND status IN "
                      f"({','.join('?' * len(ACTIVE_RECOVERY_STATES))})",
                      (session_id, session_id, *ACTIVE_RECOVERY_STATES)).fetchall()
    return [{"recoveryId": row[0], "status": row[1]} for row in rows]


def _native_state(cfg, entry):
    """The native turn state of a worker, read-only. Unavailable is not idle."""
    try:
        return opencode.state(entry)
    except Exception as error:  # noqa: BLE001 - any failure means unverifiable
        return {"state": "unavailable", "error": str(error)}


def drain_session(db, session_id, note=""):
    """Stop dispatching to a worker and report the work it still owns.

    A drain never cancels, releases or drops a task. A queued task that is bound
    to the worker is reported as outstanding, so the operator can re-route or
    resolve it explicitly. The native session and its history are untouched.
    """
    cfg = paths.config()
    entry = _roster_entry(cfg, session_id)
    outstanding = _outstanding_tasks(db, session_id)
    state = _native_state(cfg, entry)
    draining = bool(entry.get("draining", False))
    if not draining:
        updated = paths.config()
        target = _roster_entry(updated, session_id)
        target["draining"] = True
        paths.write_config(updated)
    receipt = db.execute("SELECT request_key FROM opencode_sessions WHERE session_id=?",
                         (session_id,)).fetchone()
    if receipt is not None:
        _record_session(db, session_id, receipt[0],
                        {"model": entry.get("model"), "account": entry.get("databasePath"),
                         "cwd": entry.get("cwd"), "root": str(_resolve(paths.ROOT)),
                         "dispatchEnabled": bool(entry.get("dispatchEnabled", False))},
                        "draining", "draining")
    emit(db, "opencode_session_draining", sessionId=session_id,
         outstanding=len(outstanding), note=note)
    return {"action": "drain-opencode", "sessionId": session_id, "draining": True,
            "alreadyDraining": draining, "outstanding": outstanding,
            "nativeState": state.get("state"), "removed": False,
            "note": "the native session and its history are untouched"}


def remove_session(db, session_id, note="", allow_unverified_native=False):
    """Remove a worker from the roster, keeping its native session and history.

    The removal refuses while a task or a turn is queued, in flight or
    uncertain, while a recovery holds the session, and while the native
    identity of the session cannot be verified. Nothing is cancelled, no hold is
    released, no queue entry is touched and no native session is deleted.
    """
    cfg = paths.config()
    entry = _roster_entry(cfg, session_id)
    outstanding = _outstanding_tasks(db, session_id)
    if outstanding:
        raise ValueError("The worker still owns outstanding work: "
                         + ", ".join(f"{item['taskId']}({item['state']})"
                                     for item in outstanding))
    recoveries = _active_recovery(db, session_id)
    if recoveries:
        raise ValueError("A recovery holds this session: "
                         + ", ".join(f"{item['recoveryId']}({item['status']})"
                                     for item in recoveries))
    state = _native_state(cfg, entry)
    if state.get("state") not in {"idle", "done", "completed", "stopped"}:
        if not allow_unverified_native:
            raise ValueError("The native turn state of this worker is "
                             f"'{state.get('state')}'; the removal is refused because the "
                             "ownership of the native session cannot be verified")
    else:
        try:
            opencode.verify_native_session(session_id, entry.get("cwd"),
                                           entry.get("databasePath"))
        except (ValueError, OSError) as error:
            if not allow_unverified_native:
                raise ValueError("The native identity of this session could not be "
                                 "verified: " + str(error)) from error
    updated = paths.config()
    updated["opencode"] = [item for item in updated.get("opencode", [])
                            if item.get("sessionId") != session_id]
    paths.write_config(updated)
    receipt = db.execute("SELECT request_key FROM opencode_sessions WHERE session_id=?",
                         (session_id,)).fetchone()
    if receipt is not None:
        _record_session(db, session_id, receipt[0],
                        {"model": entry.get("model"), "account": entry.get("databasePath"),
                         "cwd": entry.get("cwd"), "root": str(_resolve(paths.ROOT)),
                         "dispatchEnabled": bool(entry.get("dispatchEnabled", False))},
                        "removed", "removed from the roster")
    emit(db, "opencode_session_removed", sessionId=session_id, note=note,
         nativeSessionKept=True)
    return {"action": "remove-opencode", "sessionId": session_id, "removed": True,
            "nativeSessionKept": True, "note": "the native session and its history are untouched",
            "logicalSessions": len(updated.get("opencode", [])),
            "admissionLimit": int(updated.get("maxConcurrent", 0)),
            "heavyJobSlots": heavy.SLOT_COUNT}
