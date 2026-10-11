"""Claude transport: managed `--print` turns (default) and opt-in native `--resume --bg`."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from . import paths
from .util import atomic, command


DEFAULT_PERMISSION_MODE = "bypassPermissions"
PERMISSION_MODES = {"default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"}


def validate_permission_mode(value):
    if not isinstance(value, str) or value not in PERMISSION_MODES:
        raise ValueError("Unsupported Claude permissionMode")
    return value


def permission_mode(slot):
    return validate_permission_mode(slot.get(
        "permissionMode", paths.config().get("claudePermissionMode", DEFAULT_PERMISSION_MODE)))


def native_session_id(sid):
    """Keep logical fleet IDs stable while giving Claude a valid native UUID."""
    if not isinstance(sid, str) or not sid:
        raise ValueError("Claude logical session ID must be a nonempty string")
    try:
        uuid.UUID(sid)
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "fleet:claude:" + sid))
    return sid


def session_dir(sid):
    return paths.CLAUDE / sid


def turn_logs(sid, turn):
    return paths.LOGS / "claude" / sid / turn


def claude_rows():
    rows = json.loads(command([paths.executable("claude"), "agents", "--json", "--all"]))
    if not isinstance(rows, list):
        raise RuntimeError("Claude inventory is not a list")
    return {r["sessionId"]: r for r in rows
            if isinstance(r, dict) and isinstance(r.get("sessionId"), str)}


def claude_state(row):
    return {"state": row.get("state", "unknown"), "cwd": row.get("cwd"),
            "progress": row.get("updatedAt", row.get("lastActivityAt"))}


def wrapper_alive(pid, request_path):
    """Avoid treating a reused or foreign PID as the managed turn wrapper."""
    try:
        os.kill(pid, 0)
        if sys.platform.startswith("linux"):
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            return os.fsencode(request_path) in argv
        return True
    except (OSError, ValueError, TypeError):
        return False


def print_state(slot):
    directory = session_dir(slot["sessionId"])
    pointer = directory / "current.json"
    if not pointer.exists():
        return {"state": "idle", "progress": 0, "cwd": slot["cwd"],
                "hasSession": slot.get("resume", True)}
    launch = json.loads(pointer.read_text())
    state_file = directory / launch["turn"] / "state.json"
    if not state_file.exists():
        return {"state": "working" if time.time() - launch["startedAt"] < 15 else "blocked",
                "progress": launch["turn"] + ":0", "cwd": slot["cwd"]}
    state = json.loads(state_file.read_text())
    # Turns recorded before relocation carry the session's original cwd.
    if state.get("sessionId") != slot["sessionId"] or state.get("cwd") not in paths.known_cwds(slot):
        raise RuntimeError("Claude managed identity/cwd mismatch")
    if state["state"] == "working":
        if not wrapper_alive(state.get("wrapperPid"), state_file.parent / "request.json"):
            state.update(state="blocked", error="Claude wrapper liveness unconfirmed")
    if launch.get("resolved") and state.get("state") in {"error", "blocked"}:
        # Reconcile both recorded failures and a dead wrapper discovered by the monitor.
        return {"state": "idle", "progress": launch["turn"] + ":" + str(state.get("progress", 0)),
                "cwd": slot["cwd"], "hasSession": state.get("hasSession", slot.get("resume", True)),
                "resolvedTurn": launch["turn"]}
    state["cwd"] = slot["cwd"]
    state["progress"] = launch["turn"] + ":" + str(state.get("progress", 0))
    return state


def prune_turn_logs(sid, keep=None):
    """Bounded diagnostic logs: keep the newest turn log directories per session."""
    keep = paths.log_limits()[2] if keep is None else keep
    root = paths.LOGS / "claude" / sid
    if not root.exists():
        return
    turns = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for old in turns[:-keep] if keep else turns:
        shutil.rmtree(old, ignore_errors=True)


def send_print(slot, message):
    current = print_state(slot)
    if current["state"] != "idle":
        raise RuntimeError("Claude managed worker is not idle")
    directory = session_dir(slot["sessionId"])
    directory.mkdir(parents=True, exist_ok=True)
    turn = str(uuid.uuid4())
    path = directory / turn
    path.mkdir()
    logs = turn_logs(slot["sessionId"], turn)
    logs.mkdir(parents=True, exist_ok=True)
    request = {"sessionId": slot["sessionId"], "cwd": slot["cwd"],
               "permissionMode": permission_mode(slot),
               "model": slot["model"], "effort": slot["effort"], "message": message,
               "resume": current.get("hasSession", slot.get("resume", True)),
               "executable": paths.executable("claude"), "logDir": str(logs),
               "timeoutSeconds": slot.get("turnTimeoutSeconds",
                                          paths.config().get("claudeTurnTimeoutSeconds", 0))}
    if "allowedTools" in slot:
        allowed = slot["allowedTools"]
        if not isinstance(allowed, list) or not all(isinstance(tool, str) and tool for tool in allowed):
            raise ValueError("allowedTools must be a list of nonempty tool names")
        request["allowedTools"] = allowed
    atomic(path / "request.json", request)
    launch = {"turn": turn, "startedAt": time.time()}
    atomic(directory / "current.json", launch)
    with (logs / "wrapper.log").open("a") as log:
        child = subprocess.Popen([sys.executable, str(paths.BIN), "claude-turn",
                                  str(path / "request.json")], cwd=slot["cwd"],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True)
    launch["wrapperPid"] = child.pid
    atomic(directory / "current.json", launch)
    prune_turn_logs(slot["sessionId"])


def send_native(slot, message):
    # Explicit opt-in only after this session's cwd and native resume are verified.
    claude = paths.executable("claude")
    before = claude_rows()
    sid = slot["sessionId"]
    row = before.get(sid)
    if not row or row.get("state") not in {"done", "stopped"} or row.get("cwd") != slot["cwd"]:
        raise RuntimeError("Claude identity, cwd, or idle state changed")
    # 'done' means the turn ended, not that its background process exited.
    # Stop the idle process before resume so the CLI does not fork a copy.
    if row["state"] == "done":
        native_id = row.get("id", sid)
        stopped = command([claude, "stop", native_id])
        if stopped.strip() != "stopped " + native_id:
            raise RuntimeError("Claude stop acknowledgement missing; no resume attempted")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = claude_rows().get(sid)
            # CLI retains 'done' for completed conversations after a successful stop.
            if (current and current.get("state") in {"done", "stopped"}
                    and not current.get("pid")):
                break
            time.sleep(.5)
        else:
            raise RuntimeError("Claude process exit unconfirmed; no resume attempted")
    output = command([claude, "--resume", sid, "--bg",
                      "--model", slot["model"], "--effort", slot["effort"],
                      "--permission-mode", permission_mode(slot), message],
                     cwd=slot["cwd"])
    session_dir(sid).mkdir(parents=True, exist_ok=True)
    atomic(session_dir(sid) / "native-resume.json",
           {"sessionId": sid, "at": time.time(), "output": output})
    after = claude_rows()
    copies = [key for key, value in after.items()
              if key not in before and key != sid and value.get("cwd") == slot["cwd"]]
    current = after.get(sid)
    if copies or not current or current.get("cwd") != slot["cwd"] or "copy" in output.lower():
        raise RuntimeError("Native resume identity unconfirmed; copies=" + repr(copies))
    if sid not in output and row.get("id", sid) not in output:
        raise RuntimeError("Native resume acknowledgement missing; inspect without retry")


def project_slug(cwd):
    return "".join(c if c.isalnum() else "-" for c in str(cwd))


def native_transcript(sid):
    hits = list((Path.home() / ".claude" / "projects").glob(f"*/{native_session_id(sid)}.jsonl"))
    return hits


def terminate_group(pid, grace=10):
    """Stop a turn and every child it spawned (children can hold the output pipe open)."""
    import signal
    import threading

    def send(sig):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
    send(signal.SIGTERM)
    timer = threading.Timer(grace, send, args=(signal.SIGKILL,))
    timer.daemon = True
    timer.start()


def run_turn(request_path):
    """Run exactly one queued Claude CLI turn; persist progress and its actual result."""
    def write(path, value):
        atomic(path, value, indent=None)

    root = request_path.parent
    request = json.loads(request_path.read_text())
    mode = validate_permission_mode(request.get("permissionMode", DEFAULT_PERMISSION_MODE))
    sid = request["sessionId"]
    native_sid = native_session_id(sid)
    logs = Path(request.get("logDir") or root)
    logs.mkdir(parents=True, exist_ok=True)
    state = {"sessionId": sid, "nativeSessionId": native_sid, "state": "working", "progress": 0,
             "cwd": request["cwd"], "model": request["model"],
             "effort": request["effort"], "hasSession": request["resume"],
             "permissionMode": mode,
             "startedAt": time.time(), "wrapperPid": os.getpid()}
    write(root / "state.json", state)
    args = [request.get("executable") or paths.executable("claude"),
            "--resume" if request["resume"] else "--session-id", native_sid,
            "--print", "--model", request["model"], "--effort", request["effort"],
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", mode, request["message"]]
    if request.get("allowedTools"):
        args[1:1] = ["--allowedTools", ",".join(request["allowedTools"])]
    result = None
    identity_error = False
    initialized = False
    try:
        with (logs / "stderr.log").open("w") as errors, (logs / "events.jsonl").open("w") as events:
            process = subprocess.Popen(args, cwd=request["cwd"], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=errors, text=True,
                                       start_new_session=True)
            state["pid"] = process.pid
            write(root / "state.json", state)
            timer = None
            if request.get("timeoutSeconds"):
                import threading
                def expire():
                    state["timedOut"] = True
                    terminate_group(process.pid)
                timer = threading.Timer(request["timeoutSeconds"], expire)
                timer.daemon = True
                timer.start()
            for line in process.stdout:
                events.write(line)
                events.flush()
                state["progress"] += len(line.encode())
                state["updatedAt"] = time.time()
                try:
                    item = json.loads(line)
                except ValueError:
                    write(root / "state.json", state)
                    continue
                if item.get("type") == "system" and item.get("subtype") == "init":
                    initialized = True
                    identity_error |= (item.get("session_id") != native_sid or
                                       item.get("model") != request["model"])
                    state["hasSession"] = not identity_error
                    if identity_error:
                        terminate_group(process.pid)
                if item.get("type") == "result":
                    result = item
                    identity_error |= item.get("session_id") != native_sid
                    write(root / "result.json", item)
                write(root / "state.json", state)
            code = process.wait()
            if timer:
                timer.cancel()
        state["exitCode"] = code
        if state.get("timedOut"):
            state.update(state="error", error="Claude turn exceeded timeoutSeconds; terminated")
        elif identity_error or not initialized:
            state.update(state="blocked", error="Claude session/model identity unconfirmed")
        elif code == 0 and result and result.get("is_error") is False and result.get("subtype") == "success":
            state["state"] = "idle"
        else:
            state.update(state="error", error="Claude turn failed; inspect result and stderr")
        state["finishedAt"] = time.time()
        state.pop("pid", None)
    except Exception as error:
        state.update(state="blocked", error=str(error), finishedAt=time.time())
    write(root / "state.json", state)
