"""Pi transport: one detached controller per lane owns the existing Pi RPC session.
No inference on startup; prompts are forwarded only when the manager dispatches."""
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

from . import paths


def _slot(lane):
    pi, _ = paths.slots()
    matches = [s for s in pi if s["lane"] == lane]
    if len(matches) != 1:
        raise SystemExit(f"Lane {lane!r} is not uniquely configured")
    return matches[0]


def _lock(lane):
    directory = paths.lane_dir(lane)
    directory.mkdir(parents=True, exist_ok=True)
    return (directory / "controller.lock").open("a")


def running(lane):
    """True while a serve process holds the lane lock (prevents duplicate writers)."""
    with _lock(lane) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def verify_session(slot):
    with Path(slot["sessionFile"]).open() as stream:
        header = json.loads(stream.readline())
    if header.get("id") != slot["sessionId"]:
        raise RuntimeError("Pi session header id does not match configured sessionId")
    if header.get("cwd") not in paths.known_cwds(slot):
        raise RuntimeError("Pi session header cwd matches neither cwd nor sessionCwd")
    if not Path(slot["cwd"]).is_dir():
        raise RuntimeError("Configured cwd does not exist: " + slot["cwd"])
    return header


def start(lane):
    slot = _slot(lane)
    if running(lane):
        return {"lane": lane, "alreadyRunning": True}
    sock = paths.socket_path(lane)
    if sock.exists():
        sock.unlink()  # lock is free, so no serve process owns this socket
    verify_session(slot)
    paths.executable("pi")
    logs = paths.lane_logs(lane)
    logs.mkdir(parents=True, exist_ok=True)
    paths.rotate(logs / "controller.log")
    log = (logs / "controller.log").open("ab")
    os.chmod(logs / "controller.log", 0o600)
    child = subprocess.Popen([sys.executable, str(paths.BIN), "controller", "serve", "--lane", lane],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
    return {"lane": lane, "controller_pid": child.pid, "socket": str(sock),
            "existing_session_id": slot["sessionId"]}


def serve(lane):
    slot = _slot(lane)
    lock = _lock(lane)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if another writer is live
    verify_session(slot)
    directory, logs = paths.lane_dir(lane), paths.lane_logs(lane)
    logs.mkdir(parents=True, exist_ok=True)
    sock = paths.socket_path(lane)
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.unlink(missing_ok=True)
    status = directory / "controller.json"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock))
    os.chmod(sock, 0o600)
    server.listen(8)
    server.settimeout(1)
    for name in ("events.jsonl", "stderr.log"):
        paths.rotate(logs / name)
    events = (logs / "events.jsonl").open("ab", buffering=0)
    errors = (logs / "stderr.log").open("ab", buffering=0)
    for name in ("events.jsonl", "stderr.log"):
        os.chmod(logs / name, 0o600)
    cmd = [paths.executable("pi"), "--mode", "rpc", "--provider", slot["piProvider"],
           "--model", slot["model"], "--thinking", slot["thinking"],
           "--session", slot["sessionFile"]]
    pi = subprocess.Popen(cmd, cwd=slot["cwd"], stdin=subprocess.PIPE, stdout=events,
                          stderr=errors, start_new_session=True)
    record = {"controller_pid": os.getpid(), "pi_pid": pi.pid, "socket": str(sock),
              "session_id": slot["sessionId"], "session_file": slot["sessionFile"],
              "cwd": slot["cwd"], "model": slot["model"], "provider": slot["piProvider"],
              "thinking": slot["thinking"], "state": "running", "started_unix": time.time()}
    status.write_text(json.dumps(record, indent=2) + "\n")
    os.chmod(status, 0o600)

    def terminate(*args):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    try:
        while pi.poll() is None:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(5)
                raw = b""
                while not raw.endswith(b"\n"):
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
                    if len(raw) > 1048576:
                        raise ValueError("Command too large")
                command = json.loads(raw)
                assert isinstance(command, dict) and isinstance(command.get("type"), str)
                line = raw if raw.endswith(b"\n") else raw + b"\n"
                if command["type"] != "get_state":  # state polls are not worth keeping
                    with (directory / "commands.jsonl").open("ab") as history:
                        history.write(line)
                    os.chmod(directory / "commands.jsonl", 0o600)
                pi.stdin.write(line)
                pi.stdin.flush()
                connection.sendall(json.dumps({"forwarded": True,
                                               "id": command.get("id")}).encode() + b"\n")
    finally:
        if pi.poll() is None:  # Pi exits on stdin EOF; it is idle-safe, its session file persists.
            pi.stdin.close()
            try:
                pi.wait(10)
            except subprocess.TimeoutExpired:
                pi.terminate()
                pi.wait(10)
        record.update(state="exited", exit_code=pi.poll(), ended_unix=time.time())
        status.write_text(json.dumps(record, indent=2) + "\n")
        server.close()
        sock.unlink(missing_ok=True)


def send(lane, command):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5)
    connection.connect(str(paths.socket_path(lane)))
    try:
        connection.sendall(json.dumps(command).encode() + b"\n")
        return json.loads(connection.recv(65536).decode().strip())
    finally:
        connection.close()


def verify_model(slot, state):
    if slot.get("apiProvider"):
        model = state.get("model") or {}
        if (not isinstance(model, dict) or model.get("provider") != slot["apiProvider"]
                or model.get("id") != slot["model"]):
            raise RuntimeError("Pi live provider/model mismatch; no fallback allowed")


def query(slot, prompt=None):
    """get_state (prompt None) or forward a prompt to the lane's live Pi session."""
    live = json.loads((paths.lane_dir(slot["lane"]) / "controller.json").read_text())
    expected = {"session_id": slot["sessionId"], "cwd": slot["cwd"], "model": slot["model"],
                "thinking": slot["thinking"], "provider": slot["piProvider"]}
    if live.get("state") != "running" or any(live.get(k) != v for k, v in expected.items()):
        raise RuntimeError("Pi controller/config mismatch")
    event_file = paths.lane_logs(slot["lane"]) / "events.jsonl"
    offset = event_file.stat().st_size
    payload = {"type": "prompt" if prompt is not None else "get_state",
               "id": "fleet-" + str(uuid.uuid4())}
    if prompt is not None:
        payload["message"] = prompt
    ack = send(slot["lane"], payload)
    if ack.get("forwarded") is not True:
        raise RuntimeError("Pi forwarding unconfirmed")
    if prompt is not None:
        return
    # The controller acknowledgement is not the actual Pi response.
    deadline = time.monotonic() + 6
    with event_file.open() as stream:
        stream.seek(offset)
        while time.monotonic() < deadline:
            pos = stream.tell()
            line = stream.readline()
            if not line.endswith("\n"):
                stream.seek(pos)
                time.sleep(.1)
                continue
            row = json.loads(line)
            if row.get("id") == payload["id"] and row.get("type") == "response":
                if row.get("success") is not True:
                    raise RuntimeError("Pi state request failed")
                state = row["data"]
                if state.get("sessionId") != slot["sessionId"]:
                    raise RuntimeError("Pi live session identity mismatch")
                verify_model(slot, state)
                return {"state": "streaming" if state.get("isStreaming") else
                        ("queued" if state.get("pendingMessageCount", 0) else "idle"),
                        "progress": state.get("messageCount"), "cwd": slot["cwd"],
                        "pending": state.get("pendingMessageCount", 0)}
