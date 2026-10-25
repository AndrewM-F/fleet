#!/usr/bin/env python3
"""Review-gated, one-use continuation of one empty owned session; no CLI options."""
from dataclasses import asdict, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fleet import disposable as d

HOME = Path.home().resolve()
RUN = HOME / "Developer/.codex/fleet/runs/adea-536-idle-callback-followup-20261025-r2"
PIN = {
    "session": "ses_e8c720ce7ffe00SZeLu9ixDNM8",
    "job": "adea-536-idle-callback-followup-20261025-r2",
    "reviewOwner": "01a1713e-b8bb-7138-abb2-8f2ad2d6a02b",
    "project": "156ef9474176817ca1d89b5df4ba16e30d9fb703",
    "cwd": str(HOME / "Developer/.worktrees/adea-536-idle-callback-regression-20261025"),
    "branch": "codex/536-idle-callback-proof-20261025",
    "head": "906606e53d33f62f7d3fd7bb07143df5114bd2dc",
    "config": HOME / "Developer/.codex/fleet/runs/mimo-verified-paid-fallback-20261025/dispatch-review.json",
    "configSHA": "324b7ef96dec57a56e134a12d5cf9afa8492ba8837c92bc9911f892ae6b16326",
    "taskSHA": "4ee8430dce1985ede200d5f53ddcfc3c2bb16314ef2d3e09418f6891f2f0e161",
    "runtimeSHA": "16addd2f7ad7a5fa8b9f05b607920993913b57ddc93ecb6d23ec7acef0205d32",
}
EVENT = {
    "id": "msg_1738df5d4001itoCg614vkseCf", "time": {"created": 1792940045781},
    "type": "model-switched", "model": {"id": "mimo-v2.6-flash", "providerID": "opencode-go"},
    "previous": {"id": "mimo-v2.6-flash-free", "providerID": "opencode", "variant": "default"},
}


def exclusive_record(name, value):
    """Never overwrite or clear a prior attempt, even after preflight failure."""
    if RUN.resolve() != RUN or not RUN.is_dir():
        raise d.AdmissionRefused("Continuation receipt directory changed")
    fd = os.open(RUN / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    directory_fd = os.open(RUN, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def checked_bytes(path, digest):
    if path.is_symlink() or path.resolve() != path:
        raise d.AdmissionRefused("Continuation input path changed")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise d.AdmissionRefused("Continuation input bytes changed")
    return content


def git(*args):
    return subprocess.check_output(["git", "-C", PIN["cwd"], *args], text=True).strip()


def inbox(config):
    # Supported installed operation ID; no guessed HTTP route or state mutation.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run([config.executable, "api", "session.inbox.list", "--param",
                                 "sessionID=" + PIN["session"]], stdout=output, stderr=errors,
                                timeout=20, check=False)
        output.seek(0)
        body = output.read(d.NativeExecutor._MAX_RESPONSE_BYTES + 1)
    if result.returncode or len(body) > d.NativeExecutor._MAX_RESPONSE_BYTES:
        raise d.AdmissionRefused("Owned inbox read is unverified")
    value = json.loads(body)
    if value != {"data": []}:
        raise d.AdmissionRefused("Owned inbox is not exactly empty")


def preflight(executor, config, job):
    owner = json.loads((RUN / "ownership.json").read_text())
    expected = {"jobID": PIN["job"], "implementationOwner": PIN["job"],
                "reviewOwner": PIN["reviewOwner"], "nativeSessionID": PIN["session"],
                "worktree": PIN["cwd"], "branch": PIN["branch"], "base": PIN["head"],
                "status": "ADMISSION_STOPPED_BEFORE_PROMPT; SESSION_PRESERVED; NO_RETRY",
                "workerExecuted": False, "nativeTurnID": None}
    if any(owner.get(k) != v for k, v in expected.items()):
        raise d.AdmissionRefused("Continuation owner/assignment changed")
    if (git("rev-parse", "HEAD") != PIN["head"]
            or git("branch", "--show-current") != PIN["branch"]
            or git("status", "--porcelain", "--untracked-files=all")):
        raise d.AdmissionRefused("Owned clean worktree/head/branch changed")
    executor.verify(config)  # Requires the verified free hold; no quota probe.
    if executor.job_cwd(config, job) != PIN["cwd"]:
        raise d.AdmissionRefused("Continuation job directory changed")
    sid = PIN["session"]
    session = executor._data(executor._call(config, "GET", executor._session_path(sid)))
    model = {"id": "mimo-v2.6-flash", "providerID": "opencode-go"}
    if (not isinstance(session, dict) or session.get("id") != sid
            or session.get("title") != "Fleet-disposable:" + PIN["job"]
            or session.get("projectID") != PIN["project"]
            or session.get("location") != {"directory": PIN["cwd"]}
            or session.get("model") not in (model, {**model, "variant": "default"})
            or session.get("cost") != 0
            or session.get("tokens") != {"input": 0, "output": 0, "reasoning": 0,
                                          "cache": {"read": 0, "write": 0}}):
        raise d.AdmissionRefused("Owned empty session identity/usage changed")
    sessions = executor._list_sessions(config, search="Fleet-disposable")
    if len(sessions) != 1 or sessions[0].get("id") != sid:
        raise d.AdmissionRefused("Competing or uncertain disposable session exists")
    if (executor._list_sessions(config, parentID=sid) != []
            or executor._session_tree(config, sid) != [sid] or executor._active_ids(config)):
        raise d.AdmissionRefused("Native work or children exist; admission refused")
    # Exactly the audited switch event is allowed; no user, assistant or new event.
    if executor._messages(config, sid) != [EVENT]:
        raise d.AdmissionRefused("Session history changed")
    permission = executor._call(config, "GET", executor._session_path(sid, "/permission"))
    if permission != {"data": []}:
        raise d.AdmissionRefused("Owned permission response is not exactly empty")
    inbox(config)


class OnceExecutor(d.NativeExecutor):
    def _call(self, config, method, path, body=None, **kwargs):
        if method == "POST" and path.endswith("/prompt"):
            if path != self._session_path(PIN["session"], "/prompt") or body != {
                    "text": self.job_message}:
                raise d.AdmissionRefused("Only the pinned productive prompt is authorized")
            exclusive_record("continuation-prompt-intent.json", {
                "sessionID": PIN["session"], "jobID": PIN["job"], "taskSHA": PIN["taskSHA"],
                "state": "outcome-unknown-before-request; never replay"})
            reply = super()._call(config, method, path, body, **kwargs)
            data = self._data(reply)
            if (not isinstance(data, dict) or not isinstance(data.get("id"), str)
                    or not data["id"] or not data["id"].isprintable() or len(data["id"]) > 256):
                raise d.AdmissionRefused("Prompt acceptance is uncertain; never replay")
            exclusive_record("continuation-prompt-accepted.json", {
                "sessionID": PIN["session"], "jobID": PIN["job"], "inboxID": data["id"],
                "model": d.PAID_MODEL, "worktree": PIN["cwd"],
                "implementationOwner": PIN["job"], "state": "prompt accepted; inference not yet proven"})
            return reply
        return super()._call(config, method, path, body, **kwargs)


def run_once(executor=None, cancel=None):
    checked_bytes(Path(d.__file__).resolve(), PIN["runtimeSHA"])
    config = d.Config.parse(json.loads(checked_bytes(PIN["config"], PIN["configSHA"])))
    message = checked_bytes(RUN / "task.txt", PIN["taskSHA"]).decode().rstrip("\n")
    job = d.Job.parse({"id": PIN["job"], "message": message,
                       "rootAuthorized": True, "cwd": PIN["cwd"]})
    # Direct paid selection still requires the verified free hold in verify().
    # It cannot send a free request if another actor changes the hold file.
    config = replace(config, model=d.PAID_MODEL, account="opencode-go", fallback_model=None)
    lock = d._acquire_manager_lock(config)
    try:
        exclusive_record("continuation-attempt.json", {
            "sessionID": PIN["session"], "jobID": PIN["job"], "taskSHA": PIN["taskSHA"],
            "runtimeSHA": PIN["runtimeSHA"], "state": "reserved; not execution evidence"})
        try:
            executor = executor or OnceExecutor()
            executor.job_message = message
            preflight(executor, config, job)
            result = executor.run(config, PIN["session"], job, cancel or threading.Event())
            receipt = {"sessionID": PIN["session"], "jobID": PIN["job"],
                       "result": asdict(result), "sessionPreserved": True, "retryAllowed": False}
            exclusive_record("continuation-result.json", receipt)
            return receipt  # Never create, retire, delete, or release another job.
        except BaseException as error:
            exclusive_record("continuation-result.json", {
                "sessionID": PIN["session"], "jobID": PIN["job"], "sessionPreserved": True,
                "state": "stopped-or-uncertain; operator review required; never replay",
                "errorType": type(error).__name__, "retryAllowed": False})
            raise
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit("This one-use helper accepts no arguments or session overrides")
    cancellation = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: cancellation.set())
    print(json.dumps(run_once(cancel=cancellation)), flush=True)
