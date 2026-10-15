"""OpenCode v2 transport for explicitly rostered, existing native sessions."""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from urllib.parse import quote

from . import paths
from .util import atomic

MODEL = "opencode/deepseek-v4.1-flash-free"
_SESSION_ID = re.compile(r"ses_[A-Za-z0-9_-]+\Z")
_TURN_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_POLL_SECONDS = 0.1
_TERM_GRACE_SECONDS = 2.0
_KILL_GRACE_SECONDS = 5.0
_MAX_EVENT_LINE_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class _ProcBsdInfo(ctypes.Structure):
    """The stable proc_bsdinfo fields needed to distinguish PID reuse on macOS."""
    _fields_ = [
        ("flags", ctypes.c_uint32), ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32), ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32), ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32), ("rfu", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32), ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64),
    ]


_LIBPROC = None


def _mac_process_identity(pid):
    global _LIBPROC
    if _LIBPROC is None:
        _LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    info = _ProcBsdInfo()
    proc_pidinfo = _LIBPROC.proc_pidinfo
    proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                             ctypes.c_void_p, ctypes.c_int]
    proc_pidinfo.restype = ctypes.c_int
    size = ctypes.sizeof(info)
    if proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) != size or info.pid != pid:
        return None
    path_buf = ctypes.create_string_buffer(4096)
    proc_pidpath = _LIBPROC.proc_pidpath
    proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    proc_pidpath.restype = ctypes.c_int
    path_size = proc_pidpath(pid, path_buf, len(path_buf))
    executable = os.path.realpath(path_buf.value.decode()) if path_size > 0 else None
    return {"pid": int(info.pid), "pgid": int(info.pgid),
            "start": [int(info.start_sec), int(info.start_usec)],
            "executable": executable}


def process_identity(pid):
    """Return a PID-reuse-resistant identity, or None when the OS cannot confirm it."""
    try:
        pid = int(pid)
        if sys.platform == "darwin":
            return _mac_process_identity(pid)
        if sys.platform.startswith("linux"):
            stat = Path(f"/proc/{pid}/stat").read_text()
            tail = stat[stat.rfind(")") + 1:].split()
            start_ticks = int(tail[19])
            pgid = int(tail[2])
            executable = os.path.realpath(f"/proc/{pid}/exe")
            return {"pid": pid, "pgid": pgid, "start": [start_ticks],
                    "executable": executable}
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return None


def identity_matches(expected):
    if not isinstance(expected, dict):
        return False
    current = process_identity(expected.get("pid"))
    return bool(current and current.get("pid") == expected.get("pid")
                and current.get("pgid") == expected.get("pgid")
                and current.get("start") == expected.get("start")
                and current.get("executable") == expected.get("executable"))


def _group_exists(pgid):
    try:
        os.killpg(int(pgid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_owned_group(process, identity, sig):
    """Signal only the process group whose live leader still matches our Popen child."""
    if process.returncode is not None or not identity_matches(identity):
        return False
    try:
        os.killpg(identity["pgid"], sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _group_gone(process, pgid):
    # Popen.poll() performs waitpid(WNOHANG), so our own exited group leader does
    # not remain as a zombie and make killpg(pgid, 0) look like a live group.
    process.poll()
    return not _group_exists(pgid)


def _terminate_owned_group(process, identity, grace=_TERM_GRACE_SECONDS,
                           kill_grace=_KILL_GRACE_SECONDS):
    """Stop an owned group and wait for it to disappear; never signal a stale PGID."""
    pgid = int(identity["pgid"])
    if _group_gone(process, pgid):
        return {"confirmed": True, "uncertain": False}
    if not _signal_owned_group(process, identity, signal.SIGTERM):
        # The direct child remains safe to signal through its Popen handle. The
        # group is not safe to signal if its recorded leader identity is gone.
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + kill_grace
        while time.monotonic() < deadline and not _group_gone(process, pgid):
            time.sleep(_POLL_SECONDS)
        gone = _group_gone(process, pgid)
        return {"confirmed": gone, "uncertain": not gone}

    term_deadline = time.monotonic() + grace
    while time.monotonic() < term_deadline and not _group_gone(process, pgid):
        time.sleep(_POLL_SECONDS)
    if _group_gone(process, pgid):
        return {"confirmed": True, "uncertain": False}

    if _signal_owned_group(process, identity, signal.SIGKILL):
        kill_deadline = time.monotonic() + kill_grace
        while time.monotonic() < kill_deadline and not _group_gone(process, pgid):
            time.sleep(_POLL_SECONDS)
    gone = _group_gone(process, pgid)
    return {"confirmed": gone, "uncertain": not gone}


def session_dir(sid):
    return paths.OPENCODE / sid


def turn_dir(sid, turn):
    return session_dir(sid) / "turns" / turn


def _database_path(value):
    path = Path(os.path.expanduser(value or ""))
    if not path.is_absolute():
        raise ValueError("OpenCode databasePath must be an absolute native database path")
    return path.resolve()


def verify_native_session(sid, cwd, database_path):
    """Read only the exact native session identity row; never inspect message data."""
    if not isinstance(sid, str) or not _SESSION_ID.fullmatch(sid):
        raise ValueError("OpenCode sessionId must be an existing native ses_ ID")
    db_path = _database_path(database_path)
    if not db_path.is_file():
        raise ValueError("OpenCode native session database is unavailable")
    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT id,directory,project_id,model FROM session_v2 WHERE id=?", (sid,)
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise ValueError("OpenCode native session database could not be read safely") from error
    if not row:
        raise ValueError("OpenCode native session ID does not exist; refusing to let the CLI create it")
    try:
        actual_cwd = Path(os.path.expanduser(row[1])).resolve()
    except (TypeError, OSError, RuntimeError) as error:
        raise ValueError("OpenCode native session directory is invalid") from error
    configured_cwd = Path(cwd).resolve()
    if actual_cwd != configured_cwd:
        raise ValueError("OpenCode native session directory differs from configured cwd")
    return {"databasePath": str(db_path), "projectId": row[2], "nativeModel": row[3],
            "sessionCwd": str(actual_cwd)}


def verify_slot(slot, cfg=None):
    """Validate the fixed free model and paths; session identity is checked at launch."""
    sid = slot.get("sessionId")
    if not isinstance(sid, str) or not _SESSION_ID.fullmatch(sid):
        raise ValueError("OpenCode sessionId must be an existing native ses_ ID")
    if slot.get("model") != MODEL:
        raise ValueError(f"OpenCode model must be pinned to {MODEL}; paid/provider fallbacks are not allowed")
    if slot.get("mode") not in (None, "opencode"):
        raise ValueError("OpenCode workers cannot use Claude mode settings")
    cwd = Path(os.path.expanduser(slot.get("cwd", ""))).resolve()
    session_cwd = slot.get("sessionCwd")
    if not session_cwd or Path(os.path.expanduser(session_cwd)).resolve() != cwd:
        raise ValueError("OpenCode sessionCwd must match the native session directory")
    if not cwd.is_dir():
        raise ValueError("OpenCode cwd does not exist: " + str(cwd))
    database_path = _database_path(slot.get("databasePath"))
    if not database_path.is_file():
        raise ValueError("OpenCode native session database is unavailable")
    if not isinstance(slot.get("dispatchEnabled", False), bool):
        raise ValueError("OpenCode dispatchEnabled must be boolean")
    if slot.get("agent"):
        raise ValueError("OpenCode agent overrides are unsupported")
    timeout = int(slot.get("turnTimeoutSeconds", 0))
    if timeout < 0:
        raise ValueError("OpenCode turnTimeoutSeconds must not be negative")
    executable = paths.executable("opencode", cfg)
    return {**slot, "cwd": str(cwd), "sessionCwd": str(cwd),
            "databasePath": str(database_path), "executable": str(Path(executable).resolve())}


def state(slot):
    """Report managed run state without starting or resuming a native session."""
    slot = verify_slot(slot)
    pointer = session_dir(slot["sessionId"]) / "current.json"
    if not pointer.exists():
        return {"state": "idle", "progress": 0, "cwd": slot["cwd"],
                "model": slot["model"], "provider": "opencode"}
    launch = json.loads(pointer.read_text())
    turn = launch.get("turn")
    if not isinstance(turn, str) or not _TURN_ID.fullmatch(turn):
        raise RuntimeError("OpenCode managed turn pointer is invalid")
    if (launch.get("sessionId") != slot["sessionId"] or launch.get("cwd") != slot["cwd"]
            or launch.get("model") != slot["model"]):
        raise RuntimeError("OpenCode managed session/model/cwd identity mismatch")
    path = turn_dir(slot["sessionId"], turn) / "state.json"
    if not path.exists():
        return {"state": "working" if time.time() - launch.get("startedAt", 0) < 15 else "blocked",
                "progress": turn + ":0", "cwd": slot["cwd"],
                "error": "OpenCode turn state is not available"}
    result = json.loads(path.read_text())
    if (result.get("sessionId") != slot["sessionId"] or result.get("cwd") != slot["cwd"]
            or result.get("model") != slot["model"]):
        raise RuntimeError("OpenCode turn identity/cwd/model mismatch")
    if result.get("state") == "launching":
        if time.time() - result.get("startedAt", 0) < 15:
            # A missing native PID is expected while the CLI is being spawned.
            result["state"] = "working"
        else:
            result.update(state="blocked", error="OpenCode launch identity was not confirmed")
            atomic(path, result, indent=None)
    elif result.get("state") == "working" and not identity_matches(result.get("processIdentity")):
        result.update(state="blocked", error="OpenCode process identity is unconfirmed")
        atomic(path, result, indent=None)
    result["progress"] = turn + ":" + str(result.get("progress", 0))
    result["cwd"] = slot["cwd"]
    result.pop("processIdentity", None)
    return result


def _session_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    handle = (directory / "dispatch.lock").open("a")
    os.chmod(directory / "dispatch.lock", 0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise RuntimeError("OpenCode native session already has a Fleet turn") from error
    return handle


def _verify_inherited_lock(fd, lock_path):
    """Verify the wrapper inherited this session's live exclusive flock."""
    if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
        raise ValueError("OpenCode wrapper did not inherit a valid session lock")
    lock_path = Path(lock_path)
    try:
        inherited = os.fstat(fd)
        expected = lock_path.stat()
        if (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("OpenCode wrapper inherited the wrong session lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("OpenCode wrapper inherited a contended session lock") from error
        probe = lock_path.open("a")
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        else:
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            raise ValueError("OpenCode wrapper session lock is not held")
        finally:
            probe.close()
    except OSError as error:
        raise ValueError("OpenCode wrapper session lock could not be verified") from error


def send(slot, message, task_id=None):
    """Start one locked CLI turn in an existing session; delivery is never retried."""
    slot = verify_slot(slot)
    if not slot.get("dispatchEnabled", False):
        raise RuntimeError("OpenCode dispatch is disabled for this worker")
    if not isinstance(message, str) or not message:
        raise ValueError("OpenCode task message is empty")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("OpenCode dispatch requires the Fleet task ID")
    sid = slot["sessionId"]
    directory = session_dir(sid)
    lock = _session_lock(directory)
    turn = str(uuid.uuid4())
    path = turn_dir(sid, turn)
    logs = paths.LOGS / "opencode" / sid / turn
    handed_off = False
    try:
        current = state(slot)
        if current["state"] not in {"idle", "done", "completed", "stopped"}:
            raise RuntimeError("OpenCode worker is not idle; reconcile before retry")
        native = verify_native_session(sid, slot["cwd"], slot["databasePath"])
        path.mkdir(parents=True)
        logs.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        os.chmod(logs, 0o700)
        request = {"turn": turn, "taskId": task_id, "sessionId": sid,
                   "cwd": slot["cwd"], "sessionCwd": native["sessionCwd"],
                   "databasePath": native["databasePath"], "model": slot["model"],
                   "message": message, "executable": slot["executable"],
                   "logDir": str(logs), "timeoutSeconds": int(slot.get("turnTimeoutSeconds", 0)),
                   "lockFd": lock.fileno()}
        atomic(path / "request.json", request, indent=None)
        os.chmod(path / "request.json", 0o600)
        launch = {"turn": turn, "taskId": task_id, "sessionId": sid,
                  "cwd": slot["cwd"], "model": slot["model"], "startedAt": time.time()}
        atomic(directory / "current.json", launch, indent=None)
        os.chmod(directory / "current.json", 0o600)
        try:
            with (logs / "wrapper.log").open("a") as log:
                child = subprocess.Popen([sys.executable, str(paths.BIN), "opencode-turn",
                                          str(path / "request.json")], cwd=slot["cwd"],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         start_new_session=True, pass_fds=(lock.fileno(),))
        except Exception:
            try:
                launch_path = directory / "current.json"
                if json.loads(launch_path.read_text()).get("turn") == turn:
                    launch_path.unlink()
            except (OSError, ValueError):
                pass
            raise
        # flock state is attached to the shared open-file description. Closing the
        # parent's descriptor hands the live lock to the wrapper; LOCK_UN here
        # would release it for both processes.
        handed_off = True
        launch["wrapperPid"] = child.pid
        atomic(directory / "current.json", launch, indent=None)
        os.chmod(directory / "current.json", 0o600)
    finally:
        if not handed_off:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _event_parts(item):
    part = item.get("part")
    return part if isinstance(part, dict) else {}


def _event_message_id_conflict(item):
    part = _event_parts(item)
    top_id = item.get("messageID")
    part_id = part.get("messageID")
    return (isinstance(top_id, str) and bool(top_id)
            and isinstance(part_id, str) and bool(part_id) and top_id != part_id)


def _event_message_id(item):
    """Read only the message identity attached to the event or its typed part."""
    if _event_message_id_conflict(item):
        return None
    part = _event_parts(item)
    top_id = item.get("messageID")
    part_id = part.get("messageID")
    value = part_id if isinstance(part_id, str) and part_id else top_id
    return value if isinstance(value, str) and value else None


def _event_session_ids(item):
    """Only read session IDs from v2.0.5 event envelope and its typed part metadata."""
    values = []
    session_id = item.get("sessionID")
    if isinstance(session_id, str) and session_id:
        values.append(session_id)
    part_session = _event_parts(item).get("sessionID")
    if isinstance(part_session, str) and part_session:
        values.append(part_session)
    return values


def _model_parts(item):
    """Read model IDs only from the direct event part metadata, never tool payloads."""
    part = _event_parts(item)
    provider = part.get("providerID")
    model = part.get("modelID")
    return (provider if isinstance(provider, str) else None,
            model if isinstance(model, str) else None)


def _usage(item):
    tokens = _event_parts(item).get("tokens")
    return tokens if isinstance(tokens, dict) else None


def _verified_native_completion(database_path, sid, message_id, provider, model,
                                started_at, streamed_text):
    """Verify the streamed final message against OpenCode's read-only persisted turn state."""
    if not message_id:
        return None, "OpenCode completion message ID is missing"
    db_path = _database_path(database_path)
    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT type,seq,data FROM session_message WHERE session_id=? AND id=?",
                (sid, message_id),
            ).fetchone()
            if not row or row[0] != "assistant":
                return None, "OpenCode native assistant completion is missing"
            assistant = json.loads(row[2])
            if not isinstance(assistant, dict):
                return None, "OpenCode native assistant completion is invalid"
            assistant_model = assistant.get("model")
            assistant_model = assistant_model if isinstance(assistant_model, dict) else {}
            actual_provider = assistant_model.get("providerID")
            actual_model = assistant_model.get("id")
            completion_time = assistant.get("time")
            completion_time = completion_time if isinstance(completion_time, dict) else {}
            created = completion_time.get("created")
            completed = completion_time.get("completed")
            if actual_provider != provider or actual_model != model:
                return {"provider": actual_provider, "model": actual_model,
                        "finish": assistant.get("finish")}, \
                    "OpenCode persisted provider/model mismatch; fallback calls are blocked"
            if (not isinstance(created, (int, float)) or isinstance(created, bool)
                    or created < int(started_at * 1000)):
                return None, "OpenCode native assistant completion does not belong to this turn"
            if (not isinstance(completed, (int, float)) or isinstance(completed, bool)
                    or completed < created):
                return {"provider": actual_provider, "model": actual_model,
                        "finish": assistant.get("finish")}, \
                    "OpenCode native assistant completion timestamp is missing"
            content = assistant.get("content")
            persisted_text = "".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
                and isinstance(part.get("text", ""), str)
            ) if isinstance(content, list) else ""
            if persisted_text != streamed_text:
                return {"provider": actual_provider, "model": actual_model,
                        "finish": assistant.get("finish")}, \
                    "OpenCode streamed text does not match its native assistant completion"
            finish = assistant.get("finish")
            if assistant.get("error"):
                error = assistant["error"]
                message = error.get("message") if isinstance(error, dict) else error
                return {"provider": actual_provider, "model": actual_model,
                        "finish": finish}, "OpenCode assistant failed: " + str(message)
            if finish != "stop":
                return {"provider": actual_provider, "model": actual_model,
                        "finish": finish}, "OpenCode assistant did not finish normally: " + str(finish)
            idle = connection.execute(
                "SELECT type,data FROM session_message WHERE session_id=? AND seq>? "
                "ORDER BY seq LIMIT 1", (sid, row[1]),
            ).fetchone()
            if not idle or idle[0] != "idle":
                return {"provider": actual_provider, "model": actual_model,
                        "finish": finish}, "OpenCode native idle completion is missing"
            idle_data = json.loads(idle[1])
            if not isinstance(idle_data, dict) or idle_data.get("outcome") != "succeeded":
                outcome = idle_data.get("outcome") if isinstance(idle_data, dict) else None
                return {"provider": actual_provider, "model": actual_model,
                        "finish": finish}, "OpenCode native turn outcome was not successful: " + str(outcome)
            if idle_data.get("error"):
                return {"provider": actual_provider, "model": actual_model,
                        "finish": finish}, "OpenCode native turn failed: " + str(idle_data["error"])
            return {"provider": actual_provider, "model": actual_model, "finish": finish}, None
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return None, "OpenCode native completion could not be read safely"


def _event_error(item):
    error = item.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        label = error.get("type") or error.get("name")
        if isinstance(message, str) and message:
            return message
        if isinstance(label, str) and label:
            return label
    if isinstance(error, str) and error:
        return error
    return "OpenCode emitted a fatal error event"


def _tool_use_failure(item):
    part = _event_parts(item)
    state = part.get("state")
    if not isinstance(state, dict) or state.get("status") != "error":
        return None
    error = state.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("name") or "tool failed"
    elif isinstance(error, str):
        message = error
    else:
        message = "tool failed"
    tool = part.get("tool")
    if not isinstance(tool, str):
        tool = "unknown"
    return {"tool": tool, "error": str(message)[:500]}


def _denial_reason(failure):
    message = failure["error"].casefold()
    if "permission denied" in message or "permission rejected" in message:
        return "OpenCode permission request was rejected"
    if "the user declined this tool call" in message:
        return "OpenCode required operation was declined"
    if "the user dismissed this question" in message:
        return "OpenCode interactive question was cancelled"
    return None


def _runtime_environment(request):
    root = Path(request["turnRoot"]).resolve()
    executable = str(Path(request["executable"]).resolve())
    database_path = str(_database_path(request["databasePath"]))
    runtime = root / "runtime"
    directories = {name: runtime / name for name in
                   ("home", "config", "xdg-config", "xdg-data", "xdg-cache", "xdg-state", "tmp")}
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    config = {"$schema": "https://opencode.ai/config.json", "model": MODEL,
              "small_model": MODEL, "enabled_providers": ["opencode"],
              "plugins": [], "mcp": {"servers": {}}}
    path_entries = [str(Path(executable).parent), "/opt/homebrew/bin", "/usr/local/bin",
                    "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env = {
        "HOME": str(directories["home"]),
        "PATH": os.pathsep.join(path_entries),
        "SHELL": "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/sh",
        "TERM": "dumb",
        "CI": "1",
        "TMPDIR": str(directories["tmp"]),
        "XDG_CONFIG_HOME": str(directories["xdg-config"]),
        "XDG_DATA_HOME": str(directories["xdg-data"]),
        "XDG_CACHE_HOME": str(directories["xdg-cache"]),
        "XDG_STATE_HOME": str(directories["xdg-state"]),
        "OPENCODE_CONFIG_DIR": str(directories["config"]),
        "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
        "OPENCODE_CONFIG_CONTENT": json.dumps(config, separators=(",", ":")),
        "OPENCODE_DB": database_path,
    }
    return env


def _write_status(path, status):
    atomic(path, status, indent=None)
    os.chmod(path, 0o600)


def _cancel_requested(path, turn, identity):
    if not path.exists():
        return False
    try:
        request = json.loads(path.read_text())
    except (OSError, ValueError):
        return "invalid"
    if request.get("turn") != turn or request.get("processIdentity") != identity:
        return "identity-mismatch"
    return True


def run_turn(request_path):
    """Run one isolated, pinned turn and persist its result and safe process identity."""
    request_path = Path(request_path)
    root = request_path.parent
    request = json.loads(request_path.read_text())
    sid, cwd, model, turn = (request[k] for k in ("sessionId", "cwd", "model", "turn"))
    if not isinstance(sid, str) or not _SESSION_ID.fullmatch(sid):
        raise ValueError("OpenCode request must target an existing native ses_ ID")
    if model != MODEL:
        raise ValueError("OpenCode request model is not the pinned free model")
    if request.get("agent"):
        raise ValueError("OpenCode request agent override is not supported")
    if Path(request.get("sessionCwd", "")).resolve() != Path(cwd).resolve():
        raise ValueError("OpenCode request sessionCwd does not match cwd")
    if not Path(cwd).is_dir():
        raise ValueError("OpenCode request cwd does not exist")
    if not _TURN_ID.fullmatch(str(turn)):
        raise ValueError("OpenCode request turn ID is invalid")
    session_directory = root.parent.parent
    if session_directory.name != sid or root.parent.name != "turns":
        raise ValueError("OpenCode request is outside its native session directory")
    _verify_inherited_lock(request.get("lockFd"), session_directory / "dispatch.lock")
    native = verify_native_session(sid, cwd, request.get("databasePath"))
    request["databasePath"] = native["databasePath"]
    request["turnRoot"] = str(root)
    logs = Path(request["logDir"])
    logs.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    status = {"sessionId": sid, "cwd": cwd, "model": model, "provider": "opencode",
              "taskId": request["taskId"], "state": "launching", "progress": 0,
              "startedAt": time.time(), "pid": os.getpid()}
    _write_status(state_path, status)

    expected_provider, expected_model = model.split("/", 1)
    title = ("Fleet task " + str(request["taskId"]))[:80]
    args = [request["executable"], "run", "--standalone", "--format", "json",
            "--session", sid, "--model", model, "--title", title]
    text_parts, tool_failures = [], []
    final_message_id = None
    final_message_text = []
    usage = actual_provider = actual_model = terminal_reason = None
    final_step_started = final_step_finished = False
    session_seen = False
    provider_mismatch = model_mismatch = False
    fatal_error = None
    parse_error = False
    timed_out = cancelled = False
    cleanup = {"confirmed": True, "uncertain": False}
    process = None
    process_identity_record = None
    message_bytes = request["message"].encode("utf-8")
    started = time.monotonic()
    timeout_seconds = int(request.get("timeoutSeconds", 0))

    def consume_event_line(line):
        nonlocal usage, actual_provider, actual_model, terminal_reason
        nonlocal final_step_started, final_step_finished, session_seen
        nonlocal final_message_id, final_message_text
        nonlocal provider_mismatch, model_mismatch, fatal_error, parse_error
        events.write(line.decode("utf-8", "replace"))
        events.flush()
        try:
            item = json.loads(line)
        except ValueError:
            parse_error = True
            fatal_error = "OpenCode emitted invalid JSON transport data"
            return
        if not isinstance(item, dict):
            parse_error = True
            fatal_error = "OpenCode emitted an invalid JSON event"
            return

        event_sid_values = _event_session_ids(item)
        if any(value != sid for value in event_sid_values):
            fatal_error = "OpenCode returned a different native session ID"
        else:
            session_seen |= sid in event_sid_values
        provider, model_id = _model_parts(item)
        if provider:
            actual_provider = provider
            provider_mismatch |= provider != expected_provider
        if model_id:
            actual_model = model_id
            model_mismatch |= model_id not in {expected_model, model}
        if provider_mismatch:
            fatal_error = "OpenCode provider mismatch; fallback calls are blocked"
        elif model_mismatch:
            fatal_error = "OpenCode model mismatch; fallback calls are blocked"

        event_type = item.get("type")
        part = _event_parts(item)
        if _event_message_id_conflict(item):
            fatal_error = "OpenCode message ID mismatch in event"
        message_id = _event_message_id(item)
        if event_type == "step_start":
            final_step_started = True
            final_step_finished = False
            terminal_reason = None
            if message_id:
                if message_id != final_message_id:
                    final_message_text = []
                final_message_id = message_id
        if event_type == "text":
            fragment = part.get("text")
            if isinstance(fragment, str):
                text_parts.append(fragment)
                if message_id and message_id != final_message_id:
                    final_message_id = message_id
                    final_message_text = []
                if final_message_id:
                    final_message_text.append(fragment)
        if event_type == "step_finish":
            terminal_reason = part.get("reason")
            final_step_finished = final_step_started
            usage = _usage(item) or usage
        if event_type == "error":
            fatal_error = _event_error(item)
        if event_type == "tool_use":
            failure = _tool_use_failure(item)
            if failure:
                tool_failures.append(failure)
                denial = _denial_reason(failure)
                if denial:
                    fatal_error = denial
        _write_status(state_path, status)

    try:
        with (logs / "stderr.log").open("w") as errors, (logs / "events.jsonl").open("w") as events:
            os.chmod(logs / "stderr.log", 0o600)
            os.chmod(logs / "events.jsonl", 0o600)
            env = _runtime_environment(request)
            process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=errors, text=False,
                                       env=env, start_new_session=True, bufsize=0)
            process_identity_record = process_identity(process.pid)
            if (not process_identity_record or process_identity_record.get("pid") != process.pid
                    or process_identity_record.get("pgid") != process.pid
                    or process_identity_record.get("executable") != os.path.realpath(request["executable"])):
                process.kill()
                process.wait()
                raise RuntimeError("OpenCode child process identity could not be confirmed")
            status["pid"] = process.pid
            status["processIdentity"] = process_identity_record
            status["state"] = "working"
            _write_status(state_path, status)

            selector = selectors.DefaultSelector()
            stdin_fd = process.stdin.fileno()
            stdout_fd = process.stdout.fileno()
            os.set_blocking(stdin_fd, False)
            os.set_blocking(stdout_fd, False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            input_offset = 0
            output_open = True
            input_open = True
            event_buffer = bytearray()
            try:
                while True:
                    if timeout_seconds and time.monotonic() - started >= timeout_seconds:
                        timed_out = True
                        cleanup = _terminate_owned_group(process, process_identity_record)
                        break
                    cancel = _cancel_requested(root / "cancel.json", turn, process_identity_record)
                    if cancel:
                        cancelled = True
                        if cancel == "invalid":
                            fatal_error = "OpenCode cancellation request is invalid"
                        elif cancel == "identity-mismatch":
                            fatal_error = "OpenCode cancellation process identity mismatch"
                        cleanup = _terminate_owned_group(process, process_identity_record)
                        break
                    wait = _POLL_SECONDS
                    if timeout_seconds:
                        wait = min(wait, max(0, timeout_seconds - (time.monotonic() - started)))
                    ready = selector.select(wait)
                    if not ready:
                        if not output_open and not input_open and process.poll() is not None:
                            break
                        continue

                    for key, _ in ready:
                        if key.data == "stdin":
                            try:
                                written = os.write(stdin_fd, message_bytes[
                                    input_offset:input_offset + _READ_CHUNK_BYTES])
                            except BlockingIOError:
                                continue
                            except BrokenPipeError:
                                fatal_error = "OpenCode stopped accepting the task message"
                                break
                            input_offset += written
                            if input_offset >= len(message_bytes):
                                selector.unregister(process.stdin)
                                process.stdin.close()
                                input_open = False
                            continue

                        try:
                            chunk = os.read(stdout_fd, _READ_CHUNK_BYTES)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(process.stdout)
                            output_open = False
                            if event_buffer:
                                line = bytes(event_buffer)
                                event_buffer.clear()
                                consume_event_line(line)
                        else:
                            status["progress"] += len(chunk)
                            status["updatedAt"] = time.time()
                            _write_status(state_path, status)
                            event_buffer.extend(chunk)
                            while True:
                                newline = event_buffer.find(b"\n")
                                if newline < 0:
                                    if len(event_buffer) > _MAX_EVENT_LINE_BYTES:
                                        fatal_error = "OpenCode event line exceeded the transport limit"
                                    break
                                if newline + 1 > _MAX_EVENT_LINE_BYTES:
                                    fatal_error = "OpenCode event line exceeded the transport limit"
                                    break
                                line = bytes(event_buffer[:newline + 1])
                                del event_buffer[:newline + 1]
                                consume_event_line(line)
                                if fatal_error:
                                    break
                        if fatal_error:
                            break

                    if fatal_error:
                        cleanup = _terminate_owned_group(process, process_identity_record)
                        break
                    if not output_open and not input_open and process.poll() is not None:
                        break
            finally:
                selector.close()
                process.stdout.close()
                if not process.stdin.closed:
                    process.stdin.close()
            try:
                code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Popen still owns this child PID, so direct-child kill remains safe.
                process.kill()
                try:
                    code = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    code = None
                    cleanup = {"confirmed": False, "uncertain": True}
            if code is not None and process_identity_record \
                    and _group_exists(process_identity_record["pgid"]):
                cleanup = {"confirmed": False, "uncertain": True}

        summary = "".join(text_parts).strip()
        event_terminal_reason = terminal_reason
        completion, completion_error = _verified_native_completion(
            request["databasePath"], sid, final_message_id, expected_provider,
            expected_model, status["startedAt"], "".join(final_message_text),
        )
        completion_verified = False
        if completion:
            actual_provider = completion.get("provider")
            actual_model = completion.get("model")
            terminal_reason = completion.get("finish")
            if final_step_finished and event_terminal_reason != terminal_reason:
                completion_error = "OpenCode stream and native completion finish reasons differ"
            completion_verified = completion_error is None
        if completion_error and not fatal_error:
            fatal_error = completion_error
        if timed_out:
            status.update(state="error", error="OpenCode turn exceeded timeoutSeconds; cleanup "
                          + ("confirmed" if cleanup["confirmed"] else "uncertain"))
        elif cancelled:
            status.update(state="stopped" if cleanup["confirmed"] else "blocked",
                          error=None if cleanup["confirmed"] else
                          "OpenCode cancellation cleanup could not be confirmed")
        elif fatal_error:
            status.update(state="blocked" if "mismatch" in fatal_error or "permission" in fatal_error
                          or "declined" in fatal_error or "cancelled" in fatal_error else "error",
                          error=fatal_error)
        elif cleanup["uncertain"]:
            status.update(state="blocked", error="OpenCode process-group cleanup is uncertain")
        elif code != 0 or not summary or not session_seen or not final_step_started \
                or not completion_verified or terminal_reason != "stop" or parse_error:
            status.update(state="error", error=fatal_error or
                          "OpenCode turn did not produce a verified native completion")
        else:
            status.update(state="done", requestedProvider=expected_provider,
                          requestedModel=model, actualProvider=actual_provider,
                          actualModel=actual_model)
        status["exitCode"] = code
        status["finishedAt"] = time.time()
        status["terminalReason"] = terminal_reason
        status["toolFailures"] = tool_failures
        status["cleanup"] = "confirmed" if cleanup["confirmed"] else "uncertain"
        result = {"taskId": request["taskId"], "sessionId": sid, "cwd": cwd,
                  "state": status["state"], "provider": actual_provider, "model": actual_model,
                  "expectedProvider": expected_provider, "expectedModel": model,
                  "usage": usage, "terminalReason": terminal_reason, "summary": summary,
                  "toolFailures": tool_failures, "exitCode": code, "error": status.get("error"),
                  "cleanup": status["cleanup"],
                  "finishedAt": status["finishedAt"]}
        atomic(root / "result.json", result, indent=None)
        os.chmod(root / "result.json", 0o600)
    except Exception as error:
        if process and process.returncode is None:
            if process_identity_record:
                cleanup = _terminate_owned_group(process, process_identity_record)
            if process.returncode is None:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    cleanup = {"confirmed": False, "uncertain": True}
        status.update(state="blocked", error=str(error), finishedAt=time.time(),
                      cleanup="confirmed" if cleanup["confirmed"] else "uncertain")
    finally:
        status.pop("pid", None)
        status.pop("processIdentity", None)
        _write_status(state_path, status)
    return status


def cancel(slot):
    """Request stop for the verified live turn without depending on dispatch setup."""
    if not isinstance(slot, dict):
        raise ValueError("OpenCode worker identity is unavailable")
    sid = slot.get("sessionId")
    if not isinstance(sid, str) or not _SESSION_ID.fullmatch(sid):
        raise ValueError("OpenCode sessionId must be an existing native ses_ ID")
    if slot.get("model") != MODEL:
        raise ValueError("OpenCode model identity mismatch")
    try:
        cwd = str(Path(os.path.expanduser(slot.get("cwd", ""))).resolve())
    except (OSError, RuntimeError, TypeError) as error:
        raise ValueError("OpenCode worker cwd is invalid") from error

    pointer = session_dir(sid) / "current.json"
    if not pointer.exists():
        raise ValueError("No live OpenCode turn to cancel")
    launch = json.loads(pointer.read_text())
    if (launch.get("sessionId") != sid or launch.get("cwd") != cwd
            or launch.get("model") != MODEL):
        raise ValueError("OpenCode managed session/model/cwd identity mismatch")
    turn = launch.get("turn")
    if not isinstance(turn, str) or not _TURN_ID.fullmatch(turn):
        raise ValueError("OpenCode managed turn pointer is invalid")
    state_path = turn_dir(sid, turn) / "state.json"
    if not state_path.exists():
        raise ValueError("OpenCode turn state is unavailable")
    current = json.loads(state_path.read_text())
    identity = current.get("processIdentity")
    if (current.get("sessionId") != sid or current.get("cwd") != cwd
            or current.get("model") != MODEL):
        raise ValueError("OpenCode turn identity/cwd/model mismatch")
    if current.get("state") != "working" or not identity_matches(identity):
        raise ValueError("OpenCode process identity is not live and confirmed; no signal sent")
    cancel_path = turn_dir(slot["sessionId"], turn) / "cancel.json"
    atomic(cancel_path, {"turn": turn, "processIdentity": identity,
                         "requestedAt": time.time()}, indent=None)
    os.chmod(cancel_path, 0o600)
    return {"cancelRequested": identity["pid"], "turn": turn}


def receipt(sid, task_id, slot):
    matches = []
    for request_path in (session_dir(sid) / "turns").glob("*/request.json"):
        try:
            request = json.loads(request_path.read_text())
        except (OSError, ValueError):
            continue
        if request.get("taskId") == task_id and request.get("sessionId") == sid:
            matches.append(request_path.parent)
    if len(matches) == 1 and (matches[0] / "result.json").exists():
        result = json.loads((matches[0] / "result.json").read_text())
        return {"summary": str(result.get("summary", "")), "usage": result.get("usage"),
                "provider": result.get("provider"), "model": result.get("model"),
                "expectedProvider": result.get("expectedProvider", "opencode"),
                "expectedModel": result.get("expectedModel", slot["model"]),
                "toolFailures": result.get("toolFailures", []),
                "isError": result.get("state") != "done" or result.get("exitCode") != 0,
                "subtype": result.get("terminalReason"),
                "correlation": "exact Fleet task ID and existing OpenCode session",
                "resultFile": str(matches[0] / "result.json"), "cwd": slot["cwd"]}
    return {"captureError": "No unique OpenCode result matches this task and session"}
