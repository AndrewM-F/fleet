"""OpenCode v2 transport for explicitly rostered, existing native sessions."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

from . import paths
from .util import atomic

MODEL = "opencode/deepseek-v4.1-flash-free"


def session_dir(sid):
    return paths.OPENCODE / sid


def turn_dir(sid, turn):
    return session_dir(sid) / "turns" / turn


def verify_slot(slot, cfg=None):
    """Validate the pinned free model and the operator-verified session location."""
    sid = slot.get("sessionId")
    if not isinstance(sid, str) or not re.fullmatch(r"ses_[A-Za-z0-9_-]+", sid):
        raise ValueError("OpenCode sessionId must be an existing native ses_ ID")
    if slot.get("model") != MODEL:
        raise ValueError(f"OpenCode model must be pinned to {MODEL}; paid/provider fallbacks are not allowed")
    cwd = Path(os.path.expanduser(slot.get("cwd", ""))).resolve()
    session_cwd = slot.get("sessionCwd")
    if not session_cwd or Path(os.path.expanduser(session_cwd)).resolve() != cwd:
        raise ValueError("OpenCode sessionCwd must be independently verified and match configured cwd")
    if not cwd.is_dir():
        raise ValueError("OpenCode cwd does not exist: " + str(cwd))
    if not isinstance(slot.get("dispatchEnabled", False), bool):
        raise ValueError("OpenCode dispatchEnabled must be boolean")
    if slot.get("dispatchEnabled"):
        for field in ("nativeSessionVerified", "sessionExclusiveVerified",
                      "auxiliaryModelsVerified", "freeCanaryVerified"):
            if slot.get(field) is not True:
                raise ValueError(f"OpenCode dispatch requires {field}=true after root review")
    if slot.get("agent"):
        raise ValueError("OpenCode agent overrides are unsupported until merged model settings are verified")
    paths.executable("opencode", cfg)
    return {**slot, "cwd": str(cwd), "sessionCwd": str(cwd)}


def state(slot):
    """Report managed run state without starting or resuming a native session."""
    slot = verify_slot(slot)
    pointer = session_dir(slot["sessionId"]) / "current.json"
    if not pointer.exists():
        return {"state": "idle", "progress": 0, "cwd": slot["cwd"],
                "model": slot["model"], "provider": "opencode"}
    launch = json.loads(pointer.read_text())
    if launch.get("sessionId") != slot["sessionId"] or launch.get("cwd") != slot["cwd"] \
            or launch.get("model") != slot["model"]:
        raise RuntimeError("OpenCode managed session/model/cwd identity mismatch")
    path = turn_dir(slot["sessionId"], launch["turn"]) / "state.json"
    if not path.exists():
        return {"state": "working" if time.time() - launch.get("startedAt", 0) < 15 else "blocked",
                "progress": launch["turn"] + ":0", "cwd": slot["cwd"],
                "error": "OpenCode turn state is not available"}
    result = json.loads(path.read_text())
    if result.get("sessionId") != slot["sessionId"] or result.get("cwd") != slot["cwd"] \
            or result.get("model") != slot["model"]:
        raise RuntimeError("OpenCode turn identity/cwd/model mismatch")
    if result.get("state") == "working":
        try:
            os.kill(int(result["pid"]), 0)
        except (KeyError, ProcessLookupError, PermissionError, TypeError, ValueError):
            result.update(state="blocked", error="OpenCode process liveness unconfirmed")
            atomic(path, result, indent=None)
    result["progress"] = launch["turn"] + ":" + str(result.get("progress", 0))
    result["cwd"] = slot["cwd"]
    return result


def send(slot, message, task_id=None):
    """Start a single CLI turn in an existing OpenCode session; never retries delivery."""
    slot = verify_slot(slot)
    if not slot.get("dispatchEnabled", False):
        raise RuntimeError("OpenCode dispatch is disabled for this worker")
    current = state(slot)
    if current["state"] not in {"idle", "done", "completed", "stopped"}:
        raise RuntimeError("OpenCode worker is not idle; reconcile before retry")
    if not isinstance(message, str) or not message:
        raise ValueError("OpenCode task message is empty")
    if not task_id:
        raise ValueError("OpenCode dispatch requires the Fleet task ID")
    sid = slot["sessionId"]
    directory = session_dir(sid)
    turn = str(uuid.uuid4())
    path = turn_dir(sid, turn)
    logs = paths.LOGS / "opencode" / sid / turn
    path.mkdir(parents=True)
    logs.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    os.chmod(path, 0o700)
    os.chmod(logs, 0o700)
    request = {"turn": turn, "taskId": task_id, "sessionId": sid, "cwd": slot["cwd"],
               "sessionCwd": slot["sessionCwd"], "model": slot["model"],
               "agent": slot.get("agent"), "message": message,
               "executable": paths.executable("opencode"), "logDir": str(logs),
               "timeoutSeconds": int(slot.get("turnTimeoutSeconds", 0))}
    atomic(path / "request.json", request, indent=None)
    os.chmod(path / "request.json", 0o600)
    launch = {"turn": turn, "taskId": task_id, "sessionId": sid, "cwd": slot["cwd"],
              "model": slot["model"], "startedAt": time.time()}
    atomic(directory / "current.json", launch, indent=None)
    os.chmod(directory / "current.json", 0o600)
    with (logs / "wrapper.log").open("a") as log:
        child = subprocess.Popen([sys.executable, str(paths.BIN), "opencode-turn",
                                  str(path / "request.json")], cwd=slot["cwd"],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True)
    launch["wrapperPid"] = child.pid
    atomic(directory / "current.json", launch, indent=None)
    os.chmod(directory / "current.json", 0o600)


def _nested_value(obj, keys):
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if value is not None:
                return value
        for value in obj.values():
            found = _nested_value(value, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _nested_value(value, keys)
            if found is not None:
                return found
    return None


def _model_parts(item):
    provider = _nested_value(item, ("providerID", "providerId", "provider_id"))
    model = _nested_value(item, ("modelID", "modelId", "model_id"))
    if isinstance(model, dict):
        provider = provider or model.get("providerID") or model.get("provider")
        model = model.get("id") or model.get("modelID")
    return provider, model


def _session_id(item):
    return _nested_value(item, ("sessionID", "sessionId", "session_id"))


def _usage(item):
    return _nested_value(item, ("tokens", "usage"))


def _terminate_group(pid, grace=10):
    def send(sig):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    send(signal.SIGTERM)
    timer = threading.Timer(grace, send, args=(signal.SIGKILL,))
    timer.daemon = True
    timer.start()
    return timer


def run_turn(request_path):
    """Run one pinned native session turn and persist its actual result and usage."""
    request_path = Path(request_path)
    root = request_path.parent
    request = json.loads(request_path.read_text())
    sid, cwd, model, turn = (request[k] for k in ("sessionId", "cwd", "model", "turn"))
    if not isinstance(sid, str) or not re.fullmatch(r"ses_[A-Za-z0-9_-]+", sid):
        raise ValueError("OpenCode request must target an existing native ses_ ID")
    if model != MODEL:
        raise ValueError("OpenCode request model is not the pinned free model")
    if request.get("agent"):
        raise ValueError("OpenCode request agent override is not supported")
    if Path(request.get("sessionCwd", "")).resolve() != Path(cwd).resolve():
        raise ValueError("OpenCode request sessionCwd does not match cwd")
    if not Path(cwd).is_dir():
        raise ValueError("OpenCode request cwd does not exist")
    logs = Path(request["logDir"])
    logs.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    status = {"sessionId": sid, "cwd": cwd, "model": model, "provider": "opencode",
              "taskId": request["taskId"], "state": "working", "progress": 0,
              "startedAt": time.time(), "pid": os.getpid()}
    atomic(state_path, status, indent=None)
    os.chmod(state_path, 0o600)

    expected_provider, expected_model = model.split("/", 1)
    args = [request.get("executable") or paths.executable("opencode"), "run", "--standalone",
            "--format", "json", "--session", sid, "--model", model]
    if request.get("agent"):
        args += ["--agent", request["agent"]]
    text_parts, usage, actual_provider, actual_model = [], None, None, None
    terminal_reason = None
    identity_error = None
    session_seen = False
    provider_mismatch = False
    model_mismatch = False
    observed_providers, observed_models = set(), set()
    parse_error = False
    timed_out = False
    process = None
    timer = None
    try:
        with (logs / "stderr.log").open("w") as errors, (logs / "events.jsonl").open("w") as events:
            os.chmod(logs / "stderr.log", 0o600)
            os.chmod(logs / "events.jsonl", 0o600)
            process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=errors, text=True,
                                       start_new_session=True)
            status["pid"] = process.pid
            atomic(state_path, status, indent=None)
            os.chmod(state_path, 0o600)
            if request.get("timeoutSeconds"):
                def expire():
                    nonlocal timed_out
                    timed_out = True
                    _terminate_group(process.pid)
                timer = threading.Timer(request["timeoutSeconds"], expire)
                timer.daemon = True
                timer.start()
            process.stdin.write(request["message"])
            process.stdin.close()
            for line in process.stdout:
                events.write(line)
                events.flush()
                status["progress"] += len(line.encode())
                status["updatedAt"] = time.time()
                try:
                    item = json.loads(line)
                except ValueError:
                    parse_error = True
                    atomic(state_path, status, indent=None)
                    os.chmod(state_path, 0o600)
                    continue
                event_type = item.get("type")
                part = item.get("part") or {}
                fragment = (part.get("text") if isinstance(part, dict) else None)
                if event_type == "text" and isinstance(fragment, str):
                    text_parts.append(fragment)
                provider, model_id = _model_parts(item)
                if provider:
                    actual_provider = provider
                    observed_providers.add(provider)
                    provider_mismatch |= provider != expected_provider
                if model_id:
                    actual_model = model_id
                    observed_models.add(model_id)
                    model_mismatch |= model_id not in {expected_model, model}
                event_sid = _session_id(item)
                session_seen |= event_sid == sid
                if event_sid and event_sid != sid:
                    identity_error = "OpenCode returned a different native session ID"
                directory = _nested_value(item, ("directory", "cwd"))
                if isinstance(directory, str) and Path(directory).resolve() != Path(cwd).resolve():
                    identity_error = "OpenCode session location differs from configured cwd"
                if event_type == "step_finish":
                    terminal_reason = part.get("reason") if isinstance(part, dict) else item.get("reason")
                    usage = _usage(item) or usage
                    if terminal_reason == "error":
                        identity_error = "OpenCode reported a failed model step"
                if event_type == "permission.asked":
                    identity_error = "OpenCode task requested permission and was rejected"
                if event_type == "form.created":
                    identity_error = "OpenCode task requested interactive input and was cancelled"
                if "permission" in str(event_type).lower() and any(
                        value in json.dumps(item).lower() for value in ('"reject"', '"denied"', '"rejected"')):
                    identity_error = "OpenCode task was blocked by a permission decision"
                atomic(state_path, status, indent=None)
                os.chmod(state_path, 0o600)
            code = process.wait()
            if timer:
                timer.cancel()
        status["exitCode"] = code
        status["finishedAt"] = time.time()
        summary = "".join(text_parts).strip()
        if timed_out:
            status.update(state="error", error="OpenCode turn exceeded timeoutSeconds; terminated")
        elif identity_error:
            status.update(state="blocked", error=identity_error)
        elif provider_mismatch:
            status.update(state="blocked", error="OpenCode provider mismatch; no fallback allowed")
        elif model_mismatch:
            status.update(state="blocked", error="OpenCode model mismatch; no fallback allowed")
        elif not session_seen:
            status.update(state="blocked", error="OpenCode native session ID was not present in JSON output")
        elif code != 0 or not summary or terminal_reason not in {"stop", "length"} or parse_error:
            status.update(state="error", error="OpenCode turn did not produce a verified terminal response")
        else:
            # OpenCode 2.0.5 JSON events omit provider/model IDs. The CLI argument is
            # pinned, but absent response metadata must not be reported as observed.
            status.update(state="done", requestedProvider=expected_provider,
                          requestedModel=model, actualProvider=actual_provider,
                          actualModel=actual_model,
                          observedProviders=sorted(observed_providers),
                          observedModels=sorted(observed_models))
        atomic(root / "result.json", {"taskId": request["taskId"], "sessionId": sid,
                                      "cwd": cwd, "provider": actual_provider,
                                      "model": actual_model, "expectedProvider": expected_provider,
                                      "expectedModel": model, "observedProviders": sorted(observed_providers),
                                      "observedModels": sorted(observed_models), "usage": usage,
                                      "terminalReason": terminal_reason,
                                      "summary": summary, "exitCode": code,
                                      "error": status.get("error"),
                                      "finishedAt": status["finishedAt"]}, indent=None)
        os.chmod(root / "result.json", 0o600)
    except Exception as error:
        if process and process.poll() is None:
            _terminate_group(process.pid)
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                pass
        status.update(state="blocked", error=str(error), finishedAt=time.time())
    finally:
        if timer:
            timer.cancel()
        status.pop("pid", None)
        atomic(state_path, status, indent=None)
        os.chmod(state_path, 0o600)
    return status


def cancel(slot):
    pointer = session_dir(slot["sessionId"]) / "current.json"
    if not pointer.exists():
        raise ValueError("No live OpenCode turn to cancel")
    launch = json.loads(pointer.read_text())
    if (launch.get("sessionId") != slot["sessionId"] or launch.get("cwd") != slot["cwd"]
            or launch.get("model") != slot["model"]):
        raise ValueError("OpenCode managed session/model/cwd identity mismatch")
    state_path = turn_dir(slot["sessionId"], launch["turn"]) / "state.json"
    if not state_path.exists():
        raise ValueError("OpenCode turn state is unavailable")
    current = json.loads(state_path.read_text())
    if (current.get("sessionId") != slot["sessionId"] or current.get("cwd") != slot["cwd"]
            or current.get("model") != slot["model"]):
        raise ValueError("OpenCode turn identity/cwd/model mismatch")
    if current.get("state") != "working" or not current.get("pid"):
        raise ValueError("No live OpenCode turn to cancel")
    pid = int(current["pid"])
    try:
        os.killpg(pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        raise ValueError("OpenCode process group could not be interrupted")
    return {"interrupted": pid}


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
                "observedProviders": result.get("observedProviders", []),
                "observedModels": result.get("observedModels", []),
                "isError": result.get("exitCode") != 0 or bool(result.get("error")),
                "subtype": result.get("terminalReason"),
                "correlation": "exact Fleet task ID and existing OpenCode session",
                "resultFile": str(matches[0] / "result.json"), "cwd": slot["cwd"]}
    return {"captureError": "No unique OpenCode result matches this task and session"}
