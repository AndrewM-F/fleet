"""On-demand OpenCode session lifecycle: idempotent add, drain and safe removal.

A native session is created only through the supported OpenCode CLI API
(``session.create`` on ``POST /api/session``), which is the same supported path
an operator's own CLI uses. The Fleet never writes OpenCode's internal database
and never invents a session id: the id in a receipt is the id the native server
returned, and the roster is written only after that id was read back from the
native identity row — with its working directory **and** its model.

Creating a session does not contact a provider: the request carries the pinned
model and the directory, and the native server records them. Before any live
creation the installed CLI's own OpenAPI schema is consulted, so the call this
module makes is proved to exist and to be provider-free rather than assumed.

The roster is the only source of a worker's identity, so every addition is
recorded in a durable receipt, every retry of the same request key is
idempotent, and a partial failure leaves the roster untouched while the native
sessions that were created are kept and reported. Removal never deletes a native
session and never touches a hold, a task, a queue entry or a worktree.

The properties this module guarantees, and refuses to guess:

* **An uncertain outcome stays uncertain.** A creation whose answer never
  arrived, or whose native identity cannot be read, is recorded as uncertain and
  the request stops. The next run does not adopt anything on its own: the
  operator names the native session that was created, and that exact id is
  verified before it is registered. Absence and unreadability are never proof
  that no session was created.
* **The intent is durable before the first native call**, so a crash between the
  native create and its receipt is recoverable instead of a duplicate.
* **The request key is bound to the whole request**, so a key reused with a
  different count, target, account, working directory, model, dispatch flag or
  admission limit is refused instead of silently doing something else.
* **One writer lock** serializes the roster, the queue and the dispatch claim,
  and every roster write is a compare-and-swap against the configuration the
  writer started from.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import quote

from . import heavy, model_selection, opencode, ops, paths, roster
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
# The installed CLI's own schema is consulted before a creation; it is bounded
# too, because it starts a private server.
API_PROBE_TIMEOUT_SECONDS = 120
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
# The body properties the installed CLI's schema declares for a session create.
# A payload outside this set is not sent: the call is proved against the schema
# before it is made.
_SESSION_ID = re.compile(r"ses_[A-Za-z0-9_-]+\Z")
_REQUEST_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_PROMPT_SHAPED = ("prompt", "message", "text", "content", "input")


class ProvisioningError(RuntimeError):
    """A native session could not be created; the roster was not changed."""


class ProvisioningUncertain(ProvisioningError):
    """The outcome of a native creation is unknown; the roster was not changed.

    This is not an ordinary retryable failure. The outcome stays unknown until
    the native identity is reconciled, so the request is recorded as uncertain
    and the next run stops for an explicit, verified reconciliation.
    """


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


def _payload_digest(request, count, target_total, enable_dispatch, max_concurrent):
    """The digest of the complete request, bound to the idempotency key.

    Every option that changes what the request does is part of the digest, so a
    key reused with a different count, target, account, working directory, model,
    dispatch flag or admission limit is a conflict and not a retry.
    """
    # A request for a total binds the total, not the difference it derives:
    # the difference depends on the roster at the time, and the same request
    # has to stay the same request when it is retried.
    wanted = ("total", int(target_total)) if target_total is not None else ("count", int(count))
    material = json.dumps([request["root"], request["account"], request["model"],
                           request["cwd"], wanted,
                           bool(enable_dispatch),
                           None if max_concurrent is None else int(max_concurrent)],
                          sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


def derive_request_key(request, count, target_total, enable_dispatch, max_concurrent):
    """A deterministic key for the same request, so a retry is idempotent."""
    digest = _payload_digest(request, count, target_total, enable_dispatch, max_concurrent)
    return "opencode-" + digest[:32]


def validate_request_key(value):
    if value is None:
        return None
    if not isinstance(value, str) or not _REQUEST_KEY.fullmatch(value):
        raise ValueError("The request key must be 1-128 characters of A-Z a-z 0-9 . _ : -")
    return value


def validate_reconciled_sessions(values):
    """The native session ids an operator names for an explicit reconciliation."""
    if not values:
        return []
    seen = []
    for value in values:
        if not isinstance(value, str) or not _SESSION_ID.fullmatch(value):
            raise ValueError("A reconciled native session id must be an existing native ses_ ID")
        if value in seen:
            raise ValueError("The same native session id was named twice: " + value)
        seen.append(value)
    return seen


# --- the installed CLI's own schema ----------------------------------------

def verify_native_api_support(executable, timeout=API_PROBE_TIMEOUT_SECONDS):
    """Require the installed CLI's own schema to declare a provider-free create.

    The evidence is the OpenAPI document the installed CLI serves itself. The
    probe runs with an isolated HOME and a scratch database, so the real native
    account is never opened and no session is created. The call this module
    makes is only made when the schema declares it, and declares it without a
    prompt and without a security scheme.
    """
    scratch = Path(tempfile.mkdtemp(prefix="fleet-opencode-api-"))
    try:
        for name in ("home", "tmp", "config", "data", "cache", "state"):
            (scratch / name).mkdir(parents=True, exist_ok=True)
        environment = {
            "HOME": str(scratch / "home"),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "OPENCODE_DB": str(scratch / "opencode.db"),
            "TMPDIR": str(scratch / "tmp"),
            "XDG_CONFIG_HOME": str(scratch / "config"),
            "XDG_DATA_HOME": str(scratch / "data"),
            "XDG_CACHE_HOME": str(scratch / "cache"),
            "XDG_STATE_HOME": str(scratch / "state"),
        }
        try:
            result = subprocess.run([str(executable), "api", "--standalone", "GET",
                                     "/openapi.json"], env=environment,
                                    capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise ProvisioningError("The installed OpenCode CLI did not answer its own schema "
                                    "in time; no session was created") from error
        except OSError as error:
            raise ProvisioningError("The installed OpenCode CLI could not be started: "
                                    + error.__class__.__name__) from error
        if result.returncode != 0:
            raise ProvisioningError("The installed OpenCode CLI did not serve its schema; "
                                    "no session was created")
        try:
            spec = json.loads(result.stdout)
        except ValueError as error:
            raise ProvisioningError("The installed OpenCode CLI served an unreadable schema; "
                                    "no session was created") from error
    finally:
        shutil.rmtree(str(scratch), ignore_errors=True)
    operation = (spec.get("paths", {}).get("/api/session", {}) or {}).get("post") or {}
    if operation.get("operationId") != "session.create":
        raise ProvisioningError("The installed OpenCode CLI does not expose session.create on "
                                "POST /api/session; no session was created")
    body = (operation.get("requestBody", {}).get("content", {})
            .get("application/json", {}).get("schema", {}))
    properties = body.get("properties", {}) or {}
    if body.get("required"):
        raise ProvisioningError("The installed OpenCode CLI requires fields for a session "
                                "creation that this request does not send; no session was created")
    if not {"model", "location"}.issubset(set(properties)):
        raise ProvisioningError("The installed OpenCode CLI does not declare the model and "
                                "location fields this request sends; no session was created")
    if body.get("additionalProperties") is not False:
        raise ProvisioningError("The installed OpenCode CLI does not close its session creation "
                                "body to the declared properties; no session was created")
    schemas = spec.get("components", {}).get("schemas", {})

    def resolved(name):
        return schemas.get(name) or {}

    model_ref = resolved("Model.Ref")
    if model_ref.get("type") != "object" or set(model_ref.get("required", [])) != {"id", "providerID"}:
        raise ProvisioningError("The installed OpenCode CLI declares a different model reference "
                                "than this request sends; no session was created")
    location_ref = resolved("Location.PublicRef") or resolved("Location.Ref")
    if (location_ref.get("type") != "object"
            or set(location_ref.get("required", [])) != {"directory"}):
        raise ProvisioningError("The installed OpenCode CLI declares a different location "
                                "reference than this request sends; no session was created")
    if any(any(word in name.lower() for word in _PROMPT_SHAPED) for name in properties):
        raise ProvisioningError("The installed OpenCode CLI declares a prompt-shaped field for a "
                                "session creation; no session was created")
    if operation.get("security"):
        raise ProvisioningError("The installed OpenCode CLI declares a security scheme for a "
                                "session creation; no session was created")
    return {"source": "the installed OpenCode CLI's own OpenAPI schema",
            "operationId": operation.get("operationId"), "path": "/api/session",
            "openapi": spec.get("openapi"), "bodyProperties": sorted(properties),
            "required": [], "security": [],
            "modelRef": {"id": sorted(model_ref.get("properties", {})),
                         "required": sorted(model_ref.get("required", []))},
            "locationRef": {"properties": sorted(location_ref.get("properties", {})),
                            "required": sorted(location_ref.get("required", []))},
            "additionalProperties": body.get("additionalProperties"),
            "sha256": hashlib.sha256(result.stdout.encode()).hexdigest()}


# --- native session creation ----------------------------------------------

def session_create_payload(cwd, model):
    """The request body the installed CLI's schema declares for a session create.

    The resolved schema takes the model as a ``Model.Ref`` **object** with a
    required ``id`` and ``providerID``, and the location as a
    ``Location.PublicRef`` object with a required ``directory``. The body
    allows no other property, so the request carries nothing else: no text, no
    content and no provider reference beyond the model.
    """
    provider, name = opencode._validate_model(model)
    return {"location": {"directory": str(cwd)},
            "model": {"id": name, "providerID": provider}}


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
    payload = session_create_payload(cwd, model)
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


def _identity_failure_kind(error):
    """Whether a failed identity check means "definitely wrong" or "cannot see".

    A session row that is absent, or a native database that cannot be read, is
    not proof that no session was created: the outcome is unknown. A session
    that is there with the wrong directory or the wrong model is a definite
    failure.
    """
    message = str(error)
    if "could not be read safely" in message or "does not exist" in message:
        return "uncertain"
    return "failed"


# --- receipts ---------------------------------------------------------------

def _provisioning_row(db, request_key):
    return db.execute("SELECT request_key,model,account,cwd,root,requested_count,"
                      "dispatch_enabled,status,created,updated,detail,payload_digest,creator_pid "
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
                         payload_digest="", creator_pid=0, keep_registered=False):
    """Record the state of a provisioning request.

    ``keep_registered`` refuses to move a request that is already registered
    back to an earlier state: a final intent must never overwrite the fact
    that the request was registered.
    """
    now = time.time()
    if keep_registered:
        db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest,"
            "creator_pid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(request_key) DO UPDATE SET detail=excluded.detail,"
            "updated=excluded.updated",
            (request_key, request["model"], request["account"], request["cwd"], request["root"],
             request["count"] if count is None else count, int(request["dispatchEnabled"]),
             status, now, now, detail, payload_digest, int(creator_pid)))
        db.commit()
        return
    db.execute(
        "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
        "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest,"
        "creator_pid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(request_key) DO UPDATE SET status=excluded.status,"
        "updated=excluded.updated,detail=excluded.detail,"
        "payload_digest=excluded.payload_digest",
        (request_key, request["model"], request["account"], request["cwd"], request["root"],
         request["count"] if count is None else count, int(request["dispatchEnabled"]),
         status, now, now, detail, payload_digest, int(creator_pid)))
    db.commit()


def _creator_alive(pid):
    """Whether the process that recorded an intent is still running.

    A row without a creator was written by an earlier build or by hand, so it
    is never a live attempt.
    """
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, PermissionError, TypeError, ValueError):
        return False
    return True


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


def _registered_result(db, key, request, count, entries, dispatch_enabled, note=""):
    """The result of a request that is already registered.

    The request's own receipts are its sessions. A request whose receipts are
    missing claims nothing, and it never claims the whole roster.
    """
    registered = [receipt for receipt in _session_receipts(db, key)
                  if receipt["status"] == "registered"]
    return {"action": "add-opencode", "idempotent": True, "requestKey": key,
            "created": 0, "registered": len(registered), "requested": count,
            "sessions": [{"sessionId": receipt["session_id"],
                          "dispatchEnabled": bool(dispatch_enabled)}
                         for receipt in registered],
            "model": request["model"], "account": request["account"],
            "cwd": request["cwd"], "root": request["root"],
            "logicalSessions": len(entries),
            "admissionLimit": int(paths.config().get("maxConcurrent", 0)),
            "heavyJobSlots": heavy.SLOT_COUNT, "dryRun": False,
            "status": "registered", "uncertain": False, "retryable": False,
            "note": note}


def _locked_intent(db, key, request, count, entries, digest, reconciled, current_total, note):
    """Decide, under the writer lock, what this request may do.

    The row, its payload digest and every session receipt are read again under
    the lock, so a caller that planned from a stale snapshot cannot create an
    extra session, take over another live creator, or overwrite a request another
    caller completed or left uncertain. It returns either a finished result or
    the work this caller may do.
    """
    with roster.roster_lock():
        current = _provisioning_row(db, key)
        receipts = _session_receipts(db, key)
        if current is not None:
            bound = current[11] or ""
            if bound and bound != digest:
                # The row belongs to a different request: a stale caller never
                # rewrites it.
                raise ValueError("The request key was already used for a different request "
                                 "(count, total, account, working directory, model, dispatch "
                                 "flag or admission limit). Use a new key for a different "
                                 "request.")
            if current[7] == "registered":
                # Another caller completed this request. The result is idempotent
                # and the completed row is never overwritten.
                return {"result": _registered_result(db, key, request, count, entries,
                                                     current[6], current[10])}
            if current[7] == "creating" and _creator_alive(current[12]):
                # Another caller is creating this request right now. Its receipts
                # do not make it safe to take over.
                raise ValueError("Another request with this key is in progress; retry after it "
                                 "finishes. No session was created now.")
        # The sessions this request can account for, read under the lock.
        recorded = _recorded_sessions(current)
        accounted = {receipt["session_id"] for receipt in receipts
                     if receipt["status"] == "created"} | set(recorded) | set(reconciled)
        if current is not None and current[7] in {"uncertain", "creating"} \
                and len(accounted) < count:
            # The attempt's effects are not all accounted for: the request stays
            # uncertain and blocked until the exact identities are named and
            # verified. It is never adopted, taken over or overwritten.
            raise ValueError("The previous attempt of this request is uncertain: whether a "
                             "native session was created is unknown, and absence is no proof. "
                             "Name the native session id that was created with "
                             "--reconciled-session, or reconcile it explicitly. No session was "
                             "created now.")
        # The sessions an interrupted attempt recorded are recovered and verified
        # under the same lock, so the set cannot change underneath.
        for identifier in recorded:
            if any(receipt["session_id"] == identifier for receipt in receipts):
                continue
            try:
                _verify_created(identifier, request["cwd"], request["account"],
                                request["model"])
            except (ValueError, OSError) as error:
                raise ValueError("A native session this request created before it was "
                                 "interrupted no longer verifies (" + identifier + "): "
                                 + str(error) + ". It is not ignored and no new session is "
                                 "created.") from error
            _record_session(db, identifier, key, request, "created",
                            "recovered from the request's own record")
        receipts = _session_receipts(db, key)
        pending = [receipt for receipt in receipts if receipt["status"] == "created"]
        registered = [receipt for receipt in receipts if receipt["status"] == "registered"]
        needed = count - len(pending) - len(registered)
        if needed < 0:
            needed = 0
        if needed == 0 and not pending:
            _record_provisioning(db, key, request, "registered", "nothing to add",
                                 payload_digest=digest)
            return {"result": {"action": "add-opencode", "idempotent": True,
                                "requestKey": key, "created": 0,
                                "registered": len(registered), "requested": count,
                                "sessions": [], "model": request["model"],
                                "account": request["account"], "cwd": request["cwd"],
                                "root": request["root"], "logicalSessions": current_total,
                                "admissionLimit": int(paths.config().get("maxConcurrent", 0)),
                                "heavyJobSlots": heavy.SLOT_COUNT, "dryRun": False,
                                "status": "registered", "uncertain": False, "retryable": False,
                                "rosterChanged": False, "note": "nothing to add"}}
        if needed:
            # The intent is durable before the first native call, so a crash
            # between the create and its receipt is recoverable.
            _record_provisioning(db, key, request, "creating",
                                 "intent recorded before the native creation",
                                 payload_digest=digest, creator_pid=os.getpid())
        return {"needed": needed, "pending": pending, "registered": registered}


def _recorded_sessions(row):
    """The native sessions a request recorded for itself before its roster write."""
    if row is None or not row[10]:
        return []
    try:
        payload = json.loads(row[10])
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        return []
    return [value for value in sessions
            if isinstance(value, str) and _SESSION_ID.fullmatch(value)]


def _roster_session_ids(cfg):
    return {entry.get("sessionId") for entry in cfg.get("opencode", [])
            if isinstance(entry, dict)}


def _receipted_session_ids(db):
    return {receipt["session_id"] for receipt in _session_receipts(db)}


def _reject_duplicate_identity(db, cfg, identifier, created):
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

def add_sessions(db, count=None, target_total=None, model=None, cwd=None, root=None,
                 database_path=None, request_key=None, enable_dispatch=False,
                 max_concurrent=None, dry_run=False, note="", reconciled_sessions=None):
    """Add OpenCode sessions to the roster, idempotently.

    ``count`` adds that many sessions. ``target_total`` instead states the total
    roster size the request wants to reach, and only the difference is added, so
    one command reaches a target fleet size instead of a canary plus a scale
    that overshoots it.

    The request is validated in full before anything is created, the intent is
    recorded before the first native call, and the roster is written once, after
    every session was created and read back from the native identity row. A
    partial failure leaves the roster exactly as it was.
    """
    if (count is None) == (target_total is None):
        raise ValueError("Choose exactly one of a number of sessions to add or a total roster "
                         "size to reach")
    # The writer lock is never held while a native session is created: the
    # creation takes minutes, and the dispatcher and the operator commands must
    # not wait behind it. The lock is taken to record the intent and to commit
    # the roster, and the roster write is a compare-and-swap in between.
    return _add_sessions(db, count, target_total, model, cwd, root, database_path,
                         request_key, enable_dispatch, max_concurrent, dry_run, note,
                         reconciled_sessions)


def _add_sessions(db, count, target_total, model, cwd, root, database_path, request_key,
                   enable_dispatch, max_concurrent, dry_run, note, reconciled_sessions):
    cfg = paths.config()
    config_digest = roster.config_digest()
    entries = [entry for entry in cfg.get("opencode", []) if isinstance(entry, dict)]
    current_total = len(entries)
    if target_total is not None:
        if type(target_total) is not int or target_total < 1:
            raise ValueError("The total roster size must be a positive whole number")
        count = target_total - current_total
        if count < 0:
            raise ValueError(f"The roster already holds {current_total} OpenCode workers, "
                             f"which is more than the requested total {target_total}")
        if count == 0:
            # The target is already reached: nothing is created and the request
            # is a no-op that says so.
            return {"action": "add-opencode", "idempotent": True, "requestKey": None,
                    "requested": 0, "requestedTotal": target_total,
                    "rosterTotalBefore": current_total, "toCreate": 0, "created": 0,
                    "registered": 0, "sessions": [], "model": model,
                    "account": str(_account_of(entries, database_path)), "cwd": str(cwd),
                    "root": str(_resolve(root)), "logicalSessions": current_total,
                    "admissionLimit": int(cfg.get("maxConcurrent", 0)),
                    "heavyJobSlots": heavy.SLOT_COUNT, "dryRun": False,
                    "status": "already-reached", "uncertain": False, "retryable": False,
                    "rosterChanged": False,
                    "note": "the roster already holds the requested total"}
    request = validate_request(cfg, count, model, cwd, root, database_path)
    request["dispatchEnabled"] = bool(enable_dispatch)
    reconciled = validate_reconciled_sessions(reconciled_sessions)
    digest = _payload_digest(request, count, target_total, enable_dispatch, max_concurrent)
    key = validate_request_key(request_key) or derive_request_key(
        request, count, target_total, enable_dispatch, max_concurrent)
    existing = _provisioning_row(db, key)
    if existing is not None:
        recorded = existing[11] or ""
        if not recorded:
            # A request recorded before the payload binding cannot be verified,
            # so it fails closed instead of being accepted as a retry.
            raise ValueError("The recorded request predates the payload binding and cannot be "
                             "verified; it is refused instead of being reused. Use a new key.")
        if recorded != digest:
            raise ValueError("The request key was already used for a different request "
                             "(count, total, account, working directory, model, dispatch flag "
                             "or admission limit). Use a new key for a different request.")
    # The request's own reconciliations run before the plan: a receipt that was
    # never verified is reconciled first, and the sessions the operator named are
    # verified against the native inventory. Both are this request's own
    # sessions, and both are exact identities.
    receipts = _session_receipts(db, key)
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
            if not dry_run:
                _record_session(db, receipt["session_id"], key, request, "created",
                                "reconciled: the native identity was read back")
    pending = [receipt for receipt in receipts if receipt["status"] == "created"]
    for identifier in reconciled:
        _reject_duplicate_identity(db, cfg, identifier, pending)
        try:
            _verify_created(identifier, request["cwd"], request["account"], request["model"])
        except (ValueError, OSError) as error:
            kind = _identity_failure_kind(error)
            if kind == "uncertain":
                raise ValueError("The named native session " + identifier + " cannot be read: "
                                 + str(error) + ". Absence is no proof that it does not exist, "
                                 "so it is not registered and no session was created.") from error
            raise ValueError("The named native session " + identifier + " does not match this "
                             "request: " + str(error)) from error
        if not dry_run:
            _record_session(db, identifier, key, request, "created",
                            "reconciled explicitly by the operator")
    receipts = _session_receipts(db, key)
    pending = [receipt for receipt in receipts if receipt["status"] == "created"]
    registered = [receipt for receipt in receipts if receipt["status"] == "registered"]
    # The plan reports what the request would do. The counts that decide it are
    # taken under the writer lock, against the row and the receipts as they are
    # there, so a caller that planned from a stale snapshot cannot create an
    # extra session or a subset.
    plan = {"action": "add-opencode", "idempotent": False, "requestKey": key,
            "requested": count,
            "requestedTotal": target_total,
            "rosterTotalBefore": current_total,
            "toCreate": max(0, count - len(pending) - len(registered)),
            "created": 0, "registered": len(registered),
            "alreadyRegistered": len(registered), "pendingNative": len(pending),
            "reconciled": [identifier for identifier in reconciled],
            "model": request["model"], "account": request["account"],
            "cwd": request["cwd"], "root": request["root"],
            "dispatchEnabled": request["dispatchEnabled"],
            "logicalSessions": current_total + max(0, count - len(pending) - len(registered)),
            "admissionLimit": int(cfg.get("maxConcurrent", 0)),
            "heavyJobSlots": heavy.SLOT_COUNT,
            "maxConcurrent": {"before": int(cfg.get("maxConcurrent", 0)),
                              "after": int(cfg.get("maxConcurrent", 0))},
            "requestStatus": existing[7] if existing is not None else None,
            "dryRun": bool(dry_run)}
    limit = validate_max_concurrent(int(cfg.get("maxConcurrent", 0)), max_concurrent,
                                    plan["logicalSessions"])
    if limit is not None:
        plan["maxConcurrent"]["after"] = limit
    if dry_run:
        # A dry run writes nothing at all: no receipt, no provisioning row, no
        # roster entry, no native state.
        return plan
    executable = paths.executable("opencode", cfg)
    # The creation call is proved against the installed CLI's own schema before
    # it is made: the request is only sent when the schema declares it.
    api_support = verify_native_api_support(executable)
    scratch = paths.DATA / "opencode" / "provision" / key
    scratch.mkdir(parents=True, exist_ok=True)
    # The decision is taken under the writer lock, against the row, its payload
    # digest and every session receipt as they are there.
    decision = _locked_intent(db, key, request, count, entries, digest, reconciled,
                              current_total, note)
    if "result" in decision:
        return decision["result"]
    needed = decision["needed"]
    pending = decision["pending"]
    registered = decision["registered"]
    plan = {**plan, "toCreate": needed, "registered": len(registered),
            "alreadyRegistered": len(registered), "pendingNative": len(pending),
            "logicalSessions": current_total + needed}
    created = []
    try:
        for _ in range(needed):
            session = create_native_session(executable, request["account"], request["cwd"],
                                            request["model"], scratch)
            identifier = session["id"]
            # A returned id that is already owned is rejected instead of being
            # registered a second time.
            _reject_duplicate_identity(db, paths.config(), identifier, created + pending)
            try:
                _verify_created(identifier, request["cwd"], request["account"], request["model"])
            except (ValueError, OSError) as error:
                kind = _identity_failure_kind(error)
                if kind == "uncertain":
                    # The native inventory cannot see the session. That is not
                    # proof that it was not created, so the outcome is unknown.
                    _record_session(db, identifier, key, request, "unverified", str(error))
                    _record_provisioning(db, key, request, "uncertain", str(error),
                                         payload_digest=digest)
                    emit(db, "needs_attention", category="opencode-provisioning",
                         requestKey=key, error=str(error), sessionId=identifier, uncertain=True)
                    return {**plan, "created": len(created), "registered": 0,
                            "createdNotRegistered": created + [identifier], "sessions": [],
                            "logicalSessions": current_total,
                            "status": "uncertain", "uncertain": True, "retryable": False,
                            "failed": str(error), "rosterChanged": False}
                _record_session(db, identifier, key, request, "unverified", str(error))
                raise ProvisioningError(
                    "The created native session does not match this request: "
                    + str(error)) from error
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
                "logicalSessions": current_total,
                "status": "uncertain", "uncertain": True, "retryable": False,
                "failed": str(error), "rosterChanged": False}
    except ProvisioningError as error:
        _record_provisioning(db, key, request, "failed", str(error), payload_digest=digest)
        emit(db, "needs_attention", category="opencode-provisioning", requestKey=key,
             error=str(error), created=created)
        return {**plan, "created": len(created), "registered": 0,
                "createdNotRegistered": created, "sessions": [],
                "logicalSessions": current_total,
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
            kind = _identity_failure_kind(error)
            _record_provisioning(db, key, request,
                                 "uncertain" if kind == "uncertain" else "failed",
                                 "a previously created native session no longer verifies: "
                                 + str(error), payload_digest=digest)
            emit(db, "needs_attention", category="opencode-provisioning", requestKey=key,
                 error="a previously created native session no longer verifies: " + str(error),
                 sessionId=receipt["session_id"], uncertain=(kind == "uncertain"))
            return {**plan, "created": len(created), "registered": 0,
                    "createdNotRegistered": created + [receipt["session_id"]],
                    "sessions": [], "logicalSessions": current_total,
                    "status": "uncertain" if kind == "uncertain" else "failed",
                    "uncertain": kind == "uncertain", "retryable": False,
                    "failed": "a previously created native session no longer verifies: "
                              + str(error),
                    "rosterChanged": False}
    # The exact set that is about to be registered is proved before the roster is
    # written: the requested number of distinct ids, each with the expected
    # model, none of them owned twice.
    to_register = [receipt["session_id"] for receipt in pending] + created
    if len(to_register) != count:
        raise ProvisioningError("The request would register " + str(len(to_register))
                                 + " sessions but asked for " + str(count) + "; nothing was "
                                 "written")
    if len(set(to_register)) != len(to_register):
        raise ProvisioningError("The request would register the same native session twice; "
                                 "nothing was written")
    # The ids are recorded against the request before the roster is written, so a
    # crash between the two is recoverable: the retry registers exactly these
    # sessions instead of creating a second set.
    _record_provisioning(db, key, request, "creating",
                         json.dumps({"sessions": to_register}), payload_digest=digest,
                         creator_pid=os.getpid(), keep_registered=True)
    with roster.roster_lock():
        current = _provisioning_row(db, key)
        if current is not None:
            if current[7] == "registered":
                # Another caller completed the request while this one was
                # creating: the completed row and the roster are never
                # overwritten by a stale caller.
                return _registered_result(db, key, request, count, entries, current[6],
                                          current[10])
            if (current[12] not in (0, os.getpid())
                    and _creator_alive(current[12])):
                # Another live caller took the intent over while this one was
                # creating: it does not write a stale roster. A creator that is
                # gone is a recovery, and the recovery may write.
                raise ValueError("Another caller took over this request; nothing was written.")
        updated = paths.config()
        for identifier in to_register:
            if any(entry.get("sessionId") == identifier for entry in updated.get("opencode", [])):
                continue
            updated.setdefault("opencode", []).append({
                "sessionId": identifier, "cwd": request["cwd"], "sessionCwd": request["cwd"],
                "databasePath": request["account"], "model": request["model"],
                "provider": "opencode", "dispatchEnabled": request["dispatchEnabled"]})
        if limit is not None:
            updated["maxConcurrent"] = limit
        roster.write_config_cas(updated, config_digest)
        for identifier in to_register:
            _record_session(db, identifier, key, request, "registered", "in the roster")
        _record_provisioning(db, key, request, "registered", "roster updated",
                             payload_digest=digest)
        emit(db, "opencode_sessions_added", requestKey=key, sessions=to_register,
             model=request["model"], account=request["account"], note=note)
        proof = _prove_registration(db, updated, key, request, to_register)
    return {**plan, "created": len(created),
            "registered": len(to_register),
            "sessions": [{"sessionId": identifier,
                          "dispatchEnabled": request["dispatchEnabled"]}
                         for identifier in to_register],
            "status": "registered", "uncertain": False, "retryable": False,
            "rosterChanged": True, "proof": proof,
            "apiSupport": api_support}


def _prove_registration(db, cfg, key, request, to_register):
    """Prove the registration: the number, the distinctness and the model."""
    receipts = [receipt for receipt in _session_receipts(db, key)
                if receipt["status"] == "registered"]
    roster_ids = [entry.get("sessionId") for entry in cfg.get("opencode", [])
                  if isinstance(entry, dict)]
    models = {entry.get("model") for entry in cfg.get("opencode", [])
              if isinstance(entry, dict) and entry.get("sessionId") in to_register}
    proof = {"expected": len(to_register), "receipts": len(receipts),
             "distinct": len(set(to_register)) == len(to_register),
             "inRoster": all(identifier in roster_ids for identifier in to_register),
             "models": sorted(models)}
    if (proof["receipts"] != proof["expected"] or not proof["distinct"]
            or not proof["inRoster"] or models != {request["model"]}):
        emit(db, "needs_attention", category="opencode-provisioning", requestKey=key,
             error="the registration proof failed", proof=proof)
    return proof


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
    to the worker is reported as outstanding, and a task that is still in the
    root-authored queue file is reported as awaiting import, so the operator can
    re-route or resolve it explicitly. The native session and its history are
    untouched.

    The drain takes the same writer lock as the dispatch claim, so a claim that
    is in flight is seen by the drain, and a claim that starts after the drain
    sees the draining flag and does not launch the worker.
    """
    with roster.queue_lock():
        with roster.roster_lock():
            cfg = paths.config()
            config_digest = roster.config_digest()
            entry = _roster_entry(cfg, session_id)
            outstanding = _outstanding_tasks(db, session_id)
            try:
                awaiting = roster.queued_tasks_for(session_id, db)
            except roster.QueueUnreadable as error:
                # The queue could not be read, so the work in it is unknown. The
                # drain still stops new work, and it says what it could not see.
                awaiting = []
                queue_error = str(error)
            else:
                queue_error = None
            state = _native_state(cfg, entry)
            draining = bool(entry.get("draining", False))
            if not draining:
                updated = paths.config()
                target = _roster_entry(updated, session_id)
                target["draining"] = True
                roster.write_config_cas(updated, config_digest)
            receipt = db.execute("SELECT request_key FROM opencode_sessions WHERE session_id=?",
                                 (session_id,)).fetchone()
            if receipt is not None:
                _record_session(db, session_id, receipt[0],
                                {"model": entry.get("model"),
                                 "account": entry.get("databasePath"),
                                 "cwd": entry.get("cwd"), "root": str(_resolve(paths.ROOT)),
                                 "dispatchEnabled": bool(entry.get("dispatchEnabled", False))},
                                "draining", "draining")
            emit(db, "opencode_session_draining", sessionId=session_id,
                 outstanding=len(outstanding), awaitingImport=len(awaiting), note=note)
            return {"action": "drain-opencode", "sessionId": session_id, "draining": True,
                    "alreadyDraining": draining, "outstanding": outstanding,
                    "awaitingImport": awaiting,
                    "queueUnreadable": queue_error,
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
    uncertain, while a task for the worker is still waiting in the root-authored
    queue file, while a recovery holds the session, and while the native turn
    state explicitly says the worker is running. The operator override only
    covers a native identity that cannot be read at all; it never overrides a
    known active worker. Nothing is cancelled, no hold is released, no queue
    entry is touched and no native session is deleted.

    There is no other removal path: a receipt-owned worker leaves the roster
    only through this command, with these barriers.
    """
    with roster.queue_lock():
        with roster.roster_lock():
            with model_selection.model_lock():
                return _remove_session(db, session_id, note, allow_unverified_native)


def _remove_session(db, session_id, note, allow_unverified_native):
    cfg = paths.config()
    config_digest = roster.config_digest()
    entry = _roster_entry(cfg, session_id)
    outstanding = _outstanding_tasks(db, session_id)
    try:
        awaiting = roster.queued_tasks_for(session_id, db)
    except roster.QueueUnreadable as error:
        # An unreadable queue is not an empty queue: the removal refuses,
        # because the work in it is unknown.
        raise ValueError("The root-authored queue could not be read, so the work in it is "
                         "unknown: " + str(error) + ". The removal is refused.") from error
    if outstanding:
        raise ValueError("The worker still owns outstanding work: "
                         + ", ".join(f"{item['taskId']}({item['state']})"
                                     for item in outstanding))
    if awaiting:
        raise ValueError("The worker still has tasks waiting to be imported: "
                         + ", ".join(f"{item['taskId']}({item['state']})"
                                     for item in awaiting))
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
    roster.write_config_cas(updated, config_digest)
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
