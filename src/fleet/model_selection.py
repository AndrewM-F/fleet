"""Approved OpenCode provider/model selection for managed workers.

The roster configuration is the only source of the model a managed OpenCode worker
runs. This module reads it, reports it, and changes it under one lock that the
dispatcher also takes, so a task can never be claimed under one model and launched
under another.

Nothing here writes an OpenCode database, starts a provider process, or invents a
persistent native setter. The native session row keeps its own historical model; the
expected model for a requested next turn is kept separately and is proved by the
persisted completion, never by silence.
"""
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
import time

from . import opencode, paths

# The legacy default stays the default. The approved set is never widened or
# narrowed here: it is exactly the set the runtime already accepts.
LEGACY_DEFAULT_MODEL = opencode.MODEL


def approved_models():
    """The exact approved free model ids, and the legacy default."""
    return tuple(opencode.APPROVED_MODELS)


def validate_model(model):
    """Validate a requested model against the approved set.

    Reuses the runtime's own check so the allowlist has exactly one definition.
    """
    return opencode._validate_model(model)


@contextmanager
def model_lock():
    """The lock shared by a model selection and a dispatch claim.

    A selection and a claim/send never overlap, so the model a task is claimed under
    is the model it is launched with.
    """
    lock_path = Path(paths.DATA) / "model.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(descriptor, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as error:
        handle.close()
        raise RuntimeError("Model selection is locked by another operation") from error
    try:
        yield handle
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# The permitted-model set, the selection record and the expected model all have
# exactly one definition, in the runtime module that enforces them.
selection_of = opencode.selection_of
allowed_models = opencode.allowed_models
claim_model = opencode.expected_model


def _opencode_entries(cfg):
    return [entry for entry in cfg.get("opencode", []) if isinstance(entry, dict)]


def _worker_entry(cfg, session_id):
    matches = [entry for entry in _opencode_entries(cfg)
               if entry.get("sessionId") == session_id]
    if len(matches) != 1:
        raise ValueError("Worker must be one existing configured OpenCode session")
    return matches[0]


def _busy_states(db, session_id):
    return ("dispatching", "sent", "running", "uncertain")


def busy_workers(db, cfg=None):
    """The OpenCode workers with a claimed, running or uncertain task."""
    cfg = paths.config() if cfg is None else cfg
    busy = {}
    for entry in _opencode_entries(cfg):
        sid = entry.get("sessionId")
        if not isinstance(sid, str) or not sid:
            continue
        if db.execute("SELECT 1 FROM tasks WHERE sid=? AND state IN "
                       "('dispatching','sent','running','uncertain') LIMIT 1",
                       (sid,)).fetchone():
            busy[sid] = "active"
    return busy


def native_identity(slot):
    """The native session's own recorded model and directory, read-only.

    The native row is the session's historical identity. It is reported, never
    written, and it is not the expected model for a requested next turn.
    """
    try:
        native = opencode.verify_native_session(slot["sessionId"], slot["cwd"],
                                               slot.get("databasePath"))
    except (ValueError, OSError, KeyError) as error:
        return {"model": None, "provider": None, "directory": None,
                "error": str(error)}
    provider = native.get("nativeModelProvider")
    model = native.get("nativeModelId")
    return {"model": f"{provider}/{model}" if provider and model else None,
            "provider": provider, "directory": native.get("sessionCwd"),
            "error": None}


def inspect(db=None, cfg=None):
    """Report every configured OpenCode worker's model state. Read-only."""
    cfg = paths.config() if cfg is None else cfg
    if db is None:
        from .store import database

        db = database()
    busy = busy_workers(db, cfg)
    workers = []
    for entry in _opencode_entries(cfg):
        sid = entry.get("sessionId")
        native = native_identity(entry)
        selection = selection_of(entry)
        workers.append({
            "sessionId": sid,
            "configuredModel": entry.get("model"),
            "expectedModel": entry.get("model"),
            "nativeIdentityModel": native.get("model"),
            "nativeProvider": native.get("provider"),
            "nativeDirectory": native.get("directory"),
            "nativeError": native.get("error"),
            "dispatchEnabled": bool(entry.get("dispatchEnabled", False)),
            "state": busy.get(sid, "idle"),
            "selection": selection,
        })
    return {"approvedModels": list(approved_models()),
            "legacyDefaultModel": LEGACY_DEFAULT_MODEL,
            "workers": workers}


def plan(cfg, model, session_id=None, db=None):
    """Validate a requested model selection and report the exact change.

    Writes nothing. The returned plan is what ``apply`` would write.
    """
    cfg = paths.config() if cfg is None else cfg
    if session_id:
        targets = [_worker_entry(cfg, session_id)]
    else:
        targets = _opencode_entries(cfg)
    if not targets:
        raise ValueError("No configured OpenCode worker to select a model for")
    provider, name = validate_model(model)
    if db is None:
        from .store import database

        db = database()
    busy = busy_workers(db, cfg)
    changes = []
    for entry in targets:
        sid = entry.get("sessionId")
        current = entry.get("model")
        if current == model:
            changes.append({"sessionId": sid, "model": model, "action": "unchanged",
                            "state": busy.get(sid, "idle")})
            continue
        changes.append({
            "sessionId": sid,
            "model": model,
            "previous": current,
            "action": "select",
            "state": busy.get(sid, "idle"),
            "busy": sid in busy,
        })
    return {"model": model, "provider": provider, "modelName": name,
            "approvedModels": list(approved_models()),
            "changes": changes}


def apply(cfg, model, session_id=None, dry_run=False, db=None):
    """Record an approved model as the desired model for the next productive turn.

    The configuration is the source of truth. The selection is explicit: a worker that
    is active or uncertain keeps the model its in-flight turn was launched with, and
    the operator's request becomes the expected model for the next turn. No native
    state is written, no provider is contacted, and no hold, dispatch flag, session
    id, working directory, account or queue is touched.
    """
    if db is None:
        from .store import database

        db = database()
    with model_lock():
        cfg = paths.config()
        planned = plan(cfg, model, session_id, db)
        if dry_run:
            return {"applied": False, "dryRun": True, **planned}
        updated = False
        for change in planned["changes"]:
            if change["action"] != "select":
                continue
            entry = _worker_entry(cfg, change["sessionId"])
            previous = entry.get("model")
            entry["model"] = change["model"]
            entry["modelSelection"] = {
                "model": change["model"],
                "previous": previous,
                "requestedAt": time.time(),
                "by": "opencode-model",
            }
            updated = True
        if updated:
            tmp = paths.CONFIG.with_name(paths.CONFIG.name + ".tmp")
            tmp.write_text(json.dumps(cfg, indent=2) + "\n")
            os.replace(tmp, paths.CONFIG)
        deferred = [change["sessionId"] for change in planned["changes"] if change.get("busy")]
        return {"applied": updated, "dryRun": False, **planned,
                "deferredUntilIdle": deferred}
