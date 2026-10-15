"""Operator actions: resolve, cancel, roster edits, log upkeep and supervision."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from . import claude, opencode, paths, pi
from .store import database, emit
from .util import atomic


# --- resolve / cancel -------------------------------------------------------

def resolve_events(db, ids, note):
    rows = db.execute(f"SELECT id FROM events WHERE acked=0 AND id IN ({','.join('?' * len(ids))})",
                      ids).fetchall() if ids else []
    found = [r[0] for r in rows]
    db.executemany("UPDATE events SET acked=1 WHERE id=?", [(i,) for i in found])
    record = emit(db, "resolved", events=found, note=note)
    db.execute("UPDATE events SET acked=1 WHERE id=?", (record,))
    db.commit()
    return {"acked": found, "record": record}


def resolve_task(db, task_id, outcome, note):
    if outcome not in {"finished", "cancelled"}:
        raise ValueError("outcome must be finished or cancelled")
    row = db.execute("SELECT state,sid FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise ValueError("Task not found")
    if row[0] != "uncertain":
        raise ValueError(f"Only uncertain tasks can be resolved (task is {row[0]})")
    db.execute("UPDATE tasks SET state=?,updated=? WHERE id=?", (outcome, time.time(), task_id))
    record = emit(db, "task_resolved", taskId=task_id, sessionId=row[1], outcome=outcome, note=note)
    db.execute("UPDATE events SET acked=1 WHERE id=?", (record,))
    db.commit()
    return {"taskId": task_id, "state": outcome, "record": record}


def resolve_worker(db, sid, note):
    """Mark a failed managed Claude turn reviewed so the worker is dispatchable again."""
    slot = next((s for s in paths.slots()[1] if s["sessionId"] == sid), None)
    if not slot or slot.get("mode") != "print":
        raise ValueError("Not a managed (print-mode) Claude worker")
    state = claude.print_state(slot)
    if state["state"] not in {"error", "blocked"}:
        raise ValueError(f"Worker is {state['state']}; only error/blocked can be resolved")
    pointer = claude.session_dir(sid) / "current.json"
    launch = json.loads(pointer.read_text())
    if db.execute("SELECT 1 FROM tasks WHERE sid=? AND state IN ('dispatching','sent')", (sid,)).fetchone():
        raise ValueError("A task is still being delivered to this worker")
    launch.update(resolved=True, resolvedAt=time.time(), resolvedNote=note)
    atomic(pointer, launch, indent=None)
    record = emit(db, "worker_resolved", sessionId=sid, turn=launch["turn"], note=note)
    db.execute("UPDATE events SET acked=1 WHERE id=?", (record,))
    db.commit()
    return {"sessionId": sid, "turn": launch["turn"], "state": claude.print_state(slot)["state"]}


def cancel(db, task_id, note):
    """Stop a running task's turn. The task is marked cancelled and never resent."""
    row = db.execute("SELECT sid,state FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row and row[1] == "queued":
        changed = db.execute(
            "UPDATE tasks SET state='cancelled',updated=? WHERE id=? AND state='queued'",
            (time.time(), task_id))
        if changed.rowcount != 1:
            db.rollback()
            raise ValueError("Task state changed; inspect before cancelling")
        emit(db, "task_cancelled", taskId=task_id, sessionId=row[0], note=note)
        db.commit()
        return {"taskId": task_id, "action": {"queuedCancelled": True}}
    if not row or row[1] not in {"sent", "running"}:
        raise ValueError("Only queued/sent/running tasks can be cancelled")
    sid = row[0]
    slot = next((s for s in paths.all_slots() if s["sessionId"] == sid), None)
    if slot is None:
        raise ValueError("Worker not in configuration")
    if slot["provider"] == "pi":
        action = pi.send(slot["lane"], {"type": "abort", "id": "fleet-cancel-" + task_id})
    elif slot["provider"] == "opencode":
        action = opencode.cancel(slot)
    elif slot.get("mode") == "print":
        state = claude.print_state(slot)
        launch = json.loads((claude.session_dir(sid) / "current.json").read_text())
        turn_state = json.loads((claude.session_dir(sid) / launch["turn"] / "state.json").read_text())
        pid = turn_state.get("pid")
        if state["state"] != "working" or not pid:
            raise ValueError("No live Claude turn to cancel")
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
        action = {"terminated": pid}
    else:
        raise ValueError("Native-mode cancel is not supported; use `claude stop`")
    db.execute("UPDATE tasks SET state='cancelled',updated=? WHERE id=?", (time.time(), task_id))
    emit(db, "task_cancelled", taskId=task_id, sessionId=sid, note=note)
    db.commit()
    return {"taskId": task_id, "action": action}


# --- roster ----------------------------------------------------------------

def _write_config(cfg):
    tmp = paths.CONFIG.with_name(paths.CONFIG.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, paths.CONFIG)


def _check_cwd(cfg, cwd):
    cwd = Path(os.path.expanduser(cwd)).resolve()
    root = Path(os.path.expanduser(cfg.get("worktreeRoot", "~"))).resolve()
    if root not in cwd.parents:
        raise ValueError(f"cwd must be under worktreeRoot {root}")
    subprocess.run(["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    return str(cwd)


def _ids(cfg):
    return {s["sessionId"] for s in
            cfg.get("pi", []) + cfg.get("claude", []) + cfg.get("opencode", [])}


def add_pi(lane, cwd, session_file, provider, model, thinking, api_provider=None, role=None):
    cfg = paths.config()
    cwd = _check_cwd(cfg, cwd)
    session_file = str(Path(os.path.expanduser(session_file)).resolve())
    with open(session_file) as stream:
        header = json.loads(stream.readline())
    if header.get("type") != "session" or not header.get("id"):
        raise ValueError("Not a Pi session file")
    if header["id"] in _ids(cfg) or any(s["lane"] == lane for s in cfg.get("pi", [])):
        raise ValueError("Session or lane already configured")
    entry = {"lane": lane, "sessionId": header["id"], "cwd": cwd, "sessionFile": session_file,
             "sessionCwd": header.get("cwd"), "provider": provider, "model": model,
             "thinking": thinking}
    if api_provider:
        entry["apiProvider"] = api_provider
    if role:
        entry["role"] = role
    cfg.setdefault("pi", []).append(entry)
    _write_config(cfg)
    note = None if header.get("cwd") == cwd else "header cwd differs; run scripts/rehome-pi-session"
    return {"added": entry, "note": note}


def add_claude(session_id, cwd, model, effort, new=False, dispatch=False):
    cfg = paths.config()
    cwd = _check_cwd(cfg, cwd)
    if session_id in _ids(cfg):
        raise ValueError("Session already configured")
    if not new and not claude.native_transcript(session_id):
        raise ValueError("No native transcript for this session; pass --new to create on first turn")
    entry = {"sessionId": session_id, "cwd": cwd, "model": model, "effort": effort,
             "permissionMode": claude.permission_mode({}),
             "mode": "print", "resume": not new, "dispatchEnabled": dispatch}
    cfg.setdefault("claude", []).append(entry)
    _write_config(cfg)
    return {"added": entry}


def remove(session_id):
    cfg = paths.config()
    db = database()
    if db.execute("SELECT 1 FROM tasks WHERE sid=? AND state IN ('queued','dispatching','sent','running')",
                  (session_id,)).fetchone():
        raise ValueError("Worker has queued or in-flight tasks")
    for slot in cfg.get("pi", []):
        if slot["sessionId"] == session_id and pi.running(slot["lane"]):
            raise ValueError("Stop the lane controller first (bin/fleet stop --lane)")
    before = (len(cfg.get("pi", [])) + len(cfg.get("claude", []))
              + len(cfg.get("opencode", [])))
    cfg["pi"] = [s for s in cfg.get("pi", []) if s["sessionId"] != session_id]
    cfg["claude"] = [s for s in cfg.get("claude", []) if s["sessionId"] != session_id]
    cfg["opencode"] = [s for s in cfg.get("opencode", []) if s["sessionId"] != session_id]
    if before == len(cfg["pi"]) + len(cfg["claude"]) + len(cfg["opencode"]):
        raise ValueError("Session not configured")
    _write_config(cfg)
    return {"removed": session_id, "note": "native session history left in place"}


# --- logs ------------------------------------------------------------------

def copytruncate(path, max_bytes, keep):
    """Rotate a file that a live process appends to (O_APPEND), without restarting it."""
    path = Path(path)
    if not path.exists() or path.stat().st_size <= max_bytes:
        return False
    for n in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{n}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{n + 1}"))
    shutil.copyfile(path, path.with_name(path.name + ".1"))
    with path.open("r+b") as handle:
        handle.truncate(0)
    path.with_name(f"{path.name}.{keep + 1}").unlink(missing_ok=True)
    return True


def rotate_live_logs():
    max_bytes, keep, _ = paths.log_limits()
    rotated = []
    candidates = [paths.LOGS / "manager" / "fleet.log", paths.LOGS / "supervisor.log"]
    for slot in paths.slots()[0]:
        logs = paths.lane_logs(slot["lane"])
        candidates += [logs / "events.jsonl", logs / "stderr.log", logs / "controller.log",
                       paths.lane_dir(slot["lane"]) / "commands.jsonl"]
    for slot in paths.opencode_slots():
        candidates.append(paths.LOGS / "opencode" / slot["sessionId"] / "events.jsonl")
    for path in candidates:
        if copytruncate(path, max_bytes, keep):
            rotated.append(str(path))
    return rotated


# --- supervision -----------------------------------------------------------

def _manager_locked():
    with paths.MANAGER_LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


def start_manager():
    log_dir = paths.LOGS / "manager"
    log_dir.mkdir(parents=True, exist_ok=True)
    paths.rotate(log_dir / "fleet.log")
    with (log_dir / "fleet.log").open("a") as log:
        return subprocess.Popen([sys.executable, str(paths.BIN), "run"], cwd=paths.ROOT,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True).pid


def supervise_once(history, now=None, stale_after=180, max_restarts=3, window=600):
    """Restart dead components; give up (and raise attention) after repeated crashes."""
    now = time.time() if now is None else now
    db = database()
    actions = []

    def allowed(key):
        recent = [t for t in history.get(key, []) if now - t < window]
        history[key] = recent
        if len(recent) >= max_restarts:
            if not history.get(key + ":gaveup"):
                history[key + ":gaveup"] = now
                emit(db, "needs_attention", component=key,
                     error=f"restarted {max_restarts}x in {window}s; supervisor stopped retrying")
            return False
        history.pop(key + ":gaveup", None)
        recent.append(now)
        return True

    if (paths.ROOT / "data" / "supervisor.disabled").exists():
        return [{"skipped": "data/supervisor.disabled present"}]
    if _manager_locked():
        status = json.loads(paths.STATUS.read_text()) if paths.STATUS.exists() else {}
        age = now - status.get("updatedAt", 0)
        if age > stale_after and status.get("pid"):
            emit(db, "needs_attention", component="manager",
                 error=f"status stale for {int(age)}s; terminating hung manager")
            try:
                os.kill(int(status["pid"]), signal.SIGTERM)
            except ProcessLookupError:
                pass
            actions.append({"manager": "terminated-stale", "pid": status["pid"]})
    elif allowed("manager"):
        actions.append({"manager": "started", "pid": start_manager()})
    for slot in paths.slots()[0]:
        if pi.running(slot["lane"]) or (paths.lane_dir(slot["lane"]) / "disabled").exists():
            continue
        key = "lane:" + slot["lane"]
        if not allowed(key):
            continue
        try:
            actions.append({"lane": slot["lane"], "started": pi.start(slot["lane"])})
        except Exception as error:
            actions.append({"lane": slot["lane"], "error": str(error)})
            emit(db, "needs_attention", component=key, error=str(error))
    rotated = rotate_live_logs()
    if rotated:
        actions.append({"rotated": rotated})
    return actions


def supervise(interval=30):
    paths.DATA.mkdir(parents=True, exist_ok=True)
    with (paths.DATA / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        history = {}
        stop = []
        signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
        while not stop:
            try:
                for action in supervise_once(history):
                    print(json.dumps({"at": time.time(), **action}), flush=True)
            except Exception as error:
                print(json.dumps({"at": time.time(), "error": str(error)}), flush=True)
            for _ in range(interval):
                if stop:
                    break
                time.sleep(1)


LABEL = "dev.fleet.supervisor"


def plist_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install_launchd():
    paths.LOGS.mkdir(parents=True, exist_ok=True)
    env_path = os.environ.get("PATH", "/usr/bin:/bin")
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{sys.executable}</string><string>{paths.BIN}</string><string>supervise</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>{env_path}</string>
    <key>FLEET_ROOT</key><string>{paths.ROOT}</string>
  </dict>
  <key>WorkingDirectory</key><string>{paths.ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{paths.LOGS / 'supervisor.log'}</string>
  <key>StandardErrorPath</key><string>{paths.LOGS / 'supervisor.log'}</string>
</dict></plist>
"""
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    target.write_text(body)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(target)], check=True,
                   capture_output=True)
    return {"installed": str(target)}


def uninstall_launchd():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    plist_path().unlink(missing_ok=True)
    return {"uninstalled": LABEL}
