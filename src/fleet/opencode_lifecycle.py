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
recorded in a durable receipt, every retry of the same request key is
idempotent, and a partial failure leaves the roster untouched while the native
sessions that were created are kept and reported. Removal never deletes a native
session and never touches a hold, a task, a queue entry or a worktree.

Three properties are load bearing and are enforced here rather than assumed:

* **An uncertain outcome stays uncertain.** A creation whose answer never arrived is
  recorded as uncertain, never as an ordinary failure, and the next run reconciles
  the native identity before it creates anything.
* **The intent is durable before the first native call.** A crash between the native
  create and its receipt is reconciled from the recorded intent, so a session is
  never created twice and never left unrecorded.
* **The request key is bound to the whole request.** A key reused with a different
  count, account, working directory, model, dispatch flag or admission limit is
  refused instead of silently doing something else.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import time
from urllib.parse import quote

from . import heavy, model_selection, opencode, ops, paths
from .store import emit

# One request may add up to this many sessions. The bound keeps a single command
# from becoming unbounded fleet growth; it is deliberately above a full fleet
# scale-up (for example eight to twenty-five sessions, seventeen additions) so one
# bounded request covers it without several manual calls.
MAX_ADD_COUNT = 32
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
# A state that explicitly says the worker is working. The operator override for
# an unverifiable native identity never applies to a worker that is known to be
# running.
ACTIVE_STATES = ("running", "working", "streaming", "queued", "active")
_SESSION_ID = re.compile(r"ses_[A-Za-z0-9_-]+\Z")
_REQUEST_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


class ProvisioningError(RuntimeError):
    """A native session could not be created; the roster was not changed."""


class ProvisioningUncertain(ProvisioningError):
    """The outcome of a native creation is unknown; the roster was not changed.

    This is not an ordinary retryable failure. The outcome stays unknown until
    the native identity is reconciled, so the request is recorded as uncertain
    and the next run reconciles it before it creates anything.
    """


# --- serialization ----------------------------------------------------------

@contextmanager
def _provision_lock():
    """Serialize provisioning and roster writes across processes.

    The lock is advisory and shared by every writer of the roster through this
    module. A second process asking for the same request key waits here and then
    finds the request already provisioned, so the request is idempotent instead
    of racing.
    """
    paths.DATA.mkdir(parents=True, exist_ok=True)
    path = Path(paths.DATA) / "opencode.provision.lock"
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _config_digest():
    try:
        return hashlib.sha256(paths.CONFIG.read_bytes()).hexdigest()
    except OSError:
        return ""


def _write_config_cas(cfg, expected_digest):
    """Write the configuration only if nobody else changed it meanwhile.

    The roster is the identity source, so a concurrent writer must never be
    overwritten: the digest taken when the request started is compared again
    immediately before the write, and a changed configuration is a refusal.
    """
    if _config_digest() != expected_digest:
        raise ValueError("The configuration changed while the request was running; "
                         "nothing was written. Inspect the roster and retry.")
    paths.write_config(cfg)


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


def derive_request_key(root, account, model, cwd, count, enable_dispatch, max_concurrent):
    """A deterministic key for the same request, so a retry is idempotent."""
    return "opencode-" + _payload_digest(root, account, model, cwd, count, enable_dispatch,
                                          max_concurrent)[:32]


def _payload_digest(root, account, model, cwd, count, enable_dispatch, max_concurrent):
    """The digest of the complete request, bound to the idempotency key.

    Every option that changes what the request does is part of the digest, so a
    key reused with a different count, account, working directory, model, dispatch
    flag or admission limit is a conflict and not a retry.
    """
    material = json.dumps([str(root), str(account), str(model), str(cwd), int(count),
                           bool(enable_dispatch),
                           None if max_concurrent is None else int(max_concurrent)],
                          sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


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


def create_native_session(executable, account, cwd, model, scratch, timeout=None):
    """Create one native session through the supported OpenCode CLI API.

    The isolated runtime pins the model and its provider and keeps the native
    account database as the only durable state, exactly like a managed turn. The
    session id is the one the native server returned; a failure is reported and
    never turned into a fabricated id. A creation that does not answer is
    uncertain, not failed.
    """
    timeout = CREATE_TIMEOUT_SECONDS if timeout is None else timeout
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


def _verify_created(session_id, cwd, account, model=None):
    """Read the created session's own identity row back, read-only.

    The identity is the session id, the working directory **and** the model the
    native session itself records. A session that does not carry the requested
    model is not the session this request asked for, whatever its id says.
    """
    native = opencode.verify_native_session(session_id, cwd, account)
    if model is not None:
        opencode.verify_native_model(model, native)
    return native


def _native_candidates(account, cwd, model):
    """Read-only: the native sessions of this account for this directory and model.

    This is the reconciliation inventory. A crash between the native create and
    its receipt leaves a session the Fleet cannot name, so the next run finds
    the candidates here and adopts them instead of creating a second set.
    """
    try:
        db_path = opencode._database_path(account)
    except ValueError:
        return []
    if not db_path.is_file():
        return []
    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                "SELECT id,directory,model FROM session_v2 WHERE directory=?",
                (str(cwd),)).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []
    found = []
    for identifier, directory, native_model in rows:
        if not isinstance(identifier, str) or not _SESSION_ID.fullmatch(identifier):
            continue
        try:
            if Path(str(directory)).resolve() != _resolve(cwd):
                continue
        except (TypeError, OSError, RuntimeError):
            continue
        provider, name = opencode._native_model_parts(native_model)
        if provider and name and f"{provider}/{name}" == model:
            found.append(identifier)
    return found


# --- receipts ---------------------------------------------------------------

def _provisioning_row(db, request_key):
    return db.execute("SELECT request_key,model,account,cwd,root,requested_count,"
                      "dispatch_enabled,status,created,updated,detail,payload_digest "
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


def _record_provisioning(db, request_key, request, status, detail="", count=None,
                         payload_digest=""):
    now = time.time()
    db.execute(
        "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
        "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(request_key) DO UPDATE SET status=excluded.status,"
        "updated=excluded.updated,detail=excluded.detail,"
        "payload_digest=excluded.payload_digest",
        (request_key, request["model"], request["account"], request["cwd"], request["root"],
         request["count"] if count is None else count, int(request["dispatchEnabled"]),
         status, now, now, detail, payload_digest))
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


def _roster_session_ids(cfg):
    return {entry.get("sessionId") for entry in cfg.get("opencode", [])
            if isinstance(entry, dict)}


def _receipted_session_ids(db):
    return {receipt["session_id"] for receipt in _session_receipts(db)}


def _reject_duplicate_identity(db, cfg, identifier, request_key, created):
    """Refuse a native session id that is already owned.

    A returned id that is already in the roster, already receipted by another
    request, or already created earlier in this request is a duplicate. It is
    rejected instead of being registered a second time, and the roster is not
    changed.
    """
    if identifier in _roster_session_ids(cfg):
        raise ProvisioningError("The native server returned a session id that is already in "
                                "the roster: " + identifier)
    if identifier in created:
        raise ProvisioningError("The native server returned a session id that this request "
                                "already created: " + identifier)
    if identifier in _receipted_session_ids(db):
        raise ProvisioningError("The native server returned a session id that another "
                                "request already owns: " + identifier)


# --- reporting --------------------------------------------------------------

def status(db, root=None):
    """Report the account, the roster and the provisioning receipts. Read-only."""
    cfg = paths.config()
    entries = [entry for entry in cfg.get("opencode", []) if isinstance(entry, dict)]
    accounts = sorted({str(entry.get("databasePath")) for entry in entries
                       if entry.get("databasePath")})
    receipts = _session_receipts(db)
    registered = [receipt for receipt in receipts if receipt["status"] == "registered"]
    return {
        "action": "add-opencode-status",
        "root": str(_resolve(paths.ROOT)),
        "config": str(paths.CONFIG),
        "accounts": accounts,
        "logicalSessions": len(entries),
        "admissionLimit": int(cfg.get("maxConcurrent", 0)),
        "heavyJobSlots": heavy.SLOT_COUNT,
        "managerRunning": bool(ops._manager_locked()),
        "maxAddCount": MAX_ADD_COUNT,
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

    The request is validated in full before anything is created. The intent is
    recorded before the first native call, so a crash between the native create
    and its receipt is reconciled on the next run instead of creating a second
    session. The roster is written once, after every session was created and
    read back from the native identity row, so a partial failure leaves the
    roster exactly as it was.
    """
    with _provision_lock():
        return _add_sessions(db, count, model, cwd, root, database_path, request_key,
                             enable_dispatch, max_concurrent, dry_run, note)


def _add_sessions(db, count, model, cwd, root, database_path, request_key, enable_dispatch,
                   max_concurrent, dry_run, note):
    cfg = paths.config()
    config_digest = _config_digest()
    request = validate_request(cfg, count, model, cwd, root, database_path)
    request["dispatchEnabled"] = bool(enable_dispatch)
    digest = _payload_digest(request["root"], request["account"], request["model"],
                              request["cwd"], count, enable_dispatch, max_concurrent)
    key = validate_request_key(request_key) or derive_request_key(
        request["root"], request["account"], request["model"], request["cwd"],
        count, enable_dispatch, max_concurrent)
    entries = [entry for entry in cfg.get("opencode", []) if isinstance(entry, dict)]
    existing = _provisioning_row(db, key)
    if existing is not None:
        recorded = existing[11] or ""
        if recorded and recorded != digest:
            raise ValueError("The request key was already used for a different request "
                             "(count, account, working directory, model, dispatch flag or "
                             "admission limit). Use a new key for a different request.")
        if existing[7] == "rolled-back":
            raise ValueError("The request key belongs to a rolled-back request; it never "
                             "adds again. Use a new key for a new request.")
    receipts = _session_receipts(db, key)
    registered = [receipt for receipt in receipts if receipt["status"] == "registered"]
    if existing is not None and existing[7] == "registered" and not registered:
        # The request was recorded as registered but no session receipt survived;
        # the roster is the authority, so the sessions are counted from it.
        registered = [{"session_id": entry.get("sessionId"), "status": "registered"}
                      for entry in entries if entry.get("sessionId")]
    if existing is not None and existing[7] == "registered":
        return {"action": "add-opencode", "idempotent": True, "requestKey": key,
                "created": 0, "registered": len(registered), "requested": count,
                "sessions": [{"sessionId": receipt["session_id"],
                              "dispatchEnabled": bool(existing[6])} for receipt in registered],
                "model": request["model"], "account": request["account"],
                "cwd": request["cwd"], "root": request["root"],
                "logicalSessions": len(entries),
                "admissionLimit": int(cfg.get("maxConcurrent", 0)),
                "heavyJobSlots": heavy.SLOT_COUNT, "dryRun": False,
                "note": existing[10]}
    # A receipt whose native identity was never verified is never ignored: it is
    # reconciled first, and an unresolved one refuses the request.
    unresolved = [receipt for receipt in receipts if receipt["status"] == "unverified"]
    if unresolved:
        for receipt in unresolved:
            try:
                _verify_created(receipt["session_id"], request["cwd"], request["account"],
                                request["model"])
            except (ValueError, OSError) as error:
                raise ValueError("A native session of this request was created but its "
                                 "identity could never be verified (" + receipt["session_id"]
                                 + "): " + str(error)
                                 + ". It is not ignored and no new session is created.") from error
            _record_session(db, receipt["session_id"], key, request, "created",
                            "reconciled: the native identity was read back")
    receipts = _session_receipts(db, key)
    pending = [receipt for receipt in receipts if receipt["status"] == "created"]
    registered = [receipt for receipt in receipts if receipt["status"] == "registered"]
    reconciled = []
    if existing is not None and existing[7] in {"creating", "uncertain", "failed"}:
        # The recorded intent may already have produced native sessions whose
        # receipt was lost to a crash. They are adopted instead of recreated.
        known = _roster_session_ids(cfg) | {receipt["session_id"] for receipt in receipts}
        for candidate in _native_candidates(request["account"], request["cwd"], request["model"]):
            if candidate in known:
                continue
            _record_session(db, candidate, key, request, "created",
                            "reconciled from the recorded intent")
            reconciled.append(candidate)
        if reconciled:
            receipts = _session_receipts(db, key)
            pending = [receipt for receipt in receipts if receipt["status"] == "created"]
    needed = count - len(pending) - len(registered)
    if needed < 0:
        needed = 0
    plan = {"action": "add-opencode", "idempotent": False, "requestKey": key,
            "requested": count, "toCreate": needed,
            "created": 0, "registered": len(registered),
            "alreadyRegistered": len(registered), "pendingNative": len(pending),
            "reconciled": reconciled,
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
        _record_provisioning(db, key, request, "registered", "nothing to add",
                             payload_digest=digest)
        return {**plan, "created": 0, "registered": len(registered), "sessions": []}
    executable = paths.executable("opencode", cfg)
    scratch = paths.DATA / "opencode" / "provision" / key
    scratch.mkdir(parents=True, exist_ok=True)
    # The intent is durable before the first native call, so a crash between the
    # create and its receipt is reconciled on the next run.
    _record_provisioning(db, key, request, "creating", "intent recorded before the native "
                         "creation", payload_digest=digest)
    created = []
    try:
        for _ in range(needed):
            session = create_native_session(executable, request["account"], request["cwd"],
                                            request["model"], scratch)
            identifier = session["id"]
            # A returned id that is already owned is rejected instead of being
            # registered a second time.
            _reject_duplicate_identity(db, paths.config(), identifier, key, created)
            try:
                _verify_created(identifier, request["cwd"], request["account"], request["model"])
            except (ValueError, OSError) as error:
                # The session exists but its identity is not proven: it is recorded
                # as unverified and never counted as created.
                _record_session(db, identifier, key, request, "unverified", str(error))
                raise ProvisioningError(
                    "The created native session could not be read back: " + str(error)) from error
            _record_session(db, identifier, key, request, "created", "native session created")
            created.append(identifier)
    except ProvisioningUncertain as error:
        # The outcome is unknown. It stays unknown: it is not an ordinary failure
        # and the request is not retryable until the native identity is reconciled.
        _record_provisioning(db, key, request, "uncertain", str(error), payload_digest=digest)
        emit(db, "needs_attention", category="opencode-provisioning", requestKey=key,
             error=str(error), created=created, uncertain=True)
        return {**plan, "created": len(created), "registered": 0,
                "createdNotRegistered": created, "sessions": [],
                "logicalSessions": len(entries),
                "status": "uncertain", "uncertain": True, "retryable": False,
                "failed": str(error), "rosterChanged": False}
    except ProvisioningError as error:
        _record_provisioning(db, key, request, "failed", str(error), payload_digest=digest)
        emit(db, "needs_attention", category="opencode-provisioning", requestKey=key,
             error=str(error), created=created)
        return {**plan, "created": len(created), "registered": 0,
                "createdNotRegistered": created, "sessions": [],
                "logicalSessions": len(entries),
                "status": "failed", "uncertain": False, "retryable": True,
                "failed": str(error), "rosterChanged": False}
    # The sessions created by an earlier attempt of this request are verified
    # again, here, immediately before they are registered: their identity, their
    # working directory and their model are read back from the native inventory
    # once more, so a session that changed or vanished is never registered.
    for receipt in pending:
        try:
            _verify_created(receipt["session_id"], request["cwd"], request["account"],
                            request["model"])
        except (ValueError, OSError) as error:
            _record_provisioning(db, key, request, "failed",
                                 "a previously created native session no longer verifies: "
                                 + str(error), payload_digest=digest)
            emit(db, "needs_attention", category="opencode-provisioning", requestKey=key,
                 error="a previously created native session no longer verifies: " + str(error),
                 sessionId=receipt["session_id"])
            return {**plan, "created": len(created), "registered": 0,
                    "createdNotRegistered": created + [receipt["session_id"]],
                    "sessions": [], "logicalSessions": len(entries),
                    "status": "failed", "uncertain": False, "retryable": False,
                    "failed": "a previously created native session no longer verifies: "
                              + str(error),
                    "rosterChanged": False}
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
    _write_config_cas(updated, config_digest)
    for identifier in [receipt["session_id"] for receipt in pending] + created:
        _record_session(db, identifier, key, request, "registered", "in the roster")
    _record_provisioning(db, key, request, "registered", "roster updated", payload_digest=digest)
    emit(db, "opencode_sessions_added", requestKey=key, sessions=created,
         model=request["model"], account=request["account"], note=note)
    return {**plan, "created": len(created),
            "registered": len([receipt["session_id"] for receipt in pending]) + len(created),
            "sessions": [{"sessionId": identifier,
                          "dispatchEnabled": request["dispatchEnabled"]}
                         for identifier in ([receipt["session_id"] for receipt in pending]
                                             + created)],
            "status": "registered", "uncertain": False, "retryable": False,
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

    A manager that does not know the draining flag keeps dispatching to the
    worker, so the drain is only effective while such a manager is paused or
    upgraded. The result says so.
    """
    with _provision_lock():
        cfg = paths.config()
        config_digest = _config_digest()
        entry = _roster_entry(cfg, session_id)
        outstanding = _outstanding_tasks(db, session_id)
        state = _native_state(cfg, entry)
        draining = bool(entry.get("draining", False))
        if not draining:
            updated = paths.config()
            target = _roster_entry(updated, session_id)
            target["draining"] = True
            _write_config_cas(updated, config_digest)
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
                "note": "the native session and its history are untouched",
                "honouredByThisBuild": _manager_honours_draining(),
                "advisory": "a manager that predates the draining flag keeps dispatching to "
                            "this worker; pause the fleet or upgrade the manager for the "
                            "drain to take effect"}


def _manager_honours_draining():
    """Whether this build's dispatcher understands the draining flag.

    The running manager may be a pinned older build that reads the roster
    without the flag, so the drain is only effective against a manager that
    honours it. The result says which build the flag belongs to, and the
    rollout plan tells the operator to pause the fleet or upgrade the manager
    until then.
    """
    try:
        from . import dispatch as dispatch_module
        source = Path(dispatch_module.__file__).read_text()
    except OSError:
        return False
    return "draining" in source


def remove_session(db, session_id, note="", allow_unverified_native=False):
    """Remove a worker from the roster, keeping its native session and history.

    The removal refuses while a task or a turn is queued, in flight or
    uncertain, while a recovery holds the session, and while the native turn
    state explicitly says the worker is running. The operator override only
    covers a native identity that cannot be read at all; it never overrides a
    known active worker. Nothing is cancelled, no hold is released, no queue
    entry is touched and no native session is deleted.
    """
    with _provision_lock():
        with model_selection.model_lock():
            return _remove_session(db, session_id, note, allow_unverified_native)


def _remove_session(db, session_id, note, allow_unverified_native):
    cfg = paths.config()
    config_digest = _config_digest()
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
    native_state = state.get("state")
    if native_state in ACTIVE_STATES:
        # A worker that is known to be running is never removed, and the
        # override for an unverifiable identity does not apply to it.
        raise ValueError("The native turn state of this worker is "
                         f"'{native_state}'; a worker that is known to be active always "
                         "blocks the removal")
    if native_state not in {"idle", "done", "completed", "stopped"}:
        if not allow_unverified_native:
            raise ValueError("The native turn state of this worker could not be verified "
                             f"('{native_state}'); the removal is refused because the "
                             "ownership of the native session cannot be proven")
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
    _write_config_cas(updated, config_digest)
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


# --- rollback ---------------------------------------------------------------

def rollback_request(db, request_key, note=""):
    """Withdraw a provisioned request from the roster, keeping every native session.

    The rollback reverts the roster entries this request added and records the
    request as rolled back. The native sessions are never deleted: they stay in
    the account with their history, and the request key never adds again, so a
    retry cannot resurrect them silently.
    """
    with _provision_lock():
        cfg = paths.config()
        config_digest = _config_digest()
        row = _provisioning_row(db, request_key)
        if row is None:
            raise ValueError("The request key has no provisioning record")
        receipts = _session_receipts(db, request_key)
        added = {receipt["session_id"] for receipt in receipts
                 if receipt["status"] in {"created", "registered"}}
        updated = paths.config()
        updated["opencode"] = [entry for entry in updated.get("opencode", [])
                               if entry.get("sessionId") not in added]
        _write_config_cas(updated, config_digest)
        for receipt in receipts:
            _record_session(db, receipt["session_id"], request_key,
                            {"model": receipt["model"], "account": receipt["account"],
                             "cwd": receipt["cwd"], "root": receipt["root"],
                             "dispatchEnabled": False},
                            "rolled-back", "withdrawn from the roster")
        _record_provisioning(db, request_key,
                             {"model": row[1], "account": row[2], "cwd": row[3],
                              "root": row[4], "count": row[5],
                              "dispatchEnabled": bool(row[6])},
                             "rolled-back", note or "rolled back", payload_digest=row[11] or "")
        emit(db, "opencode_request_rolled_back", requestKey=request_key, sessions=sorted(added),
             note=note)
        return {"action": "rollback-opencode", "requestKey": request_key,
                "removedFromRoster": sorted(added),
                "nativeSessionsKept": sorted(added),
                "note": "the native sessions and their history are untouched",
                "logicalSessions": len(updated.get("opencode", [])),
                "admissionLimit": int(updated.get("maxConcurrent", 0)),
                "heavyJobSlots": heavy.SLOT_COUNT}
