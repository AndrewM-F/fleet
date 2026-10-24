"""Process-local new-job queue. No roster, ledger, recovery or session reuse."""
from collections import deque
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import quote, urlencode

MODEL = "opencode/mimo-v2.6-flash-free"
MAX_CONCURRENT = 3
MAX_QUEUED = 32
MAX_MESSAGE_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_SESSION = re.compile(r"ses_[A-Za-z0-9_-]+\Z")


class AdmissionRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    executable: str
    account: str
    cwd: str
    model: str
    max_concurrent: int

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict) or set(raw) != {
                "executable", "account", "cwd", "model", "maxConcurrent"}:
            raise AdmissionRefused("Exact disposable executable/account/cwd/model/cap required")
        if raw["model"] != MODEL or type(raw["maxConcurrent"]) is not int or not (
                1 <= raw["maxConcurrent"] <= MAX_CONCURRENT):
            raise AdmissionRefused("Only authorized MiMo free model and at most three jobs allowed")
        account = raw["account"]
        if (not isinstance(account, str) or not account.strip()
                or account != account.strip() or len(account) > 256
                or not account.isprintable()):
            raise AdmissionRefused("Account must be an opaque non-secret binding without controls")
        for key in ("executable", "cwd"):
            value = raw[key]
            if (not isinstance(value, str) or not Path(value).is_absolute()
                    or str(Path(value).resolve()) != value):
                raise AdmissionRefused("Disposable paths must be explicit and canonical")
        return cls(raw["executable"], raw["account"], raw["cwd"], raw["model"],
                   raw["maxConcurrent"])


@dataclass(frozen=True)
class Job:
    id: str
    message: str
    cwd: str | None = None

    @classmethod
    def parse(cls, raw):
        if (not isinstance(raw, dict) or set(raw) not in (
                {"id", "message", "rootAuthorized"},
                {"id", "message", "rootAuthorized", "cwd"})
                or raw["rootAuthorized"] is not True
                or not isinstance(raw["id"], str) or not _ID.fullmatch(raw["id"])
                or not isinstance(raw["message"], str) or not raw["message"].strip()
                or len(raw["message"].encode()) > MAX_MESSAGE_BYTES):
            raise AdmissionRefused("New root-authorized id/message only; session/recovery fields refused")
        cwd = raw.get("cwd")
        if cwd is not None and (
                not isinstance(cwd, str) or not Path(cwd).is_absolute()
                or str(Path(cwd).resolve()) != cwd):
            raise AdmissionRefused("Job worktree must be an absolute canonical path")
        return cls(raw["id"], raw["message"], cwd)


@dataclass(frozen=True)
class RunResult:
    # stopped means the OpenCode service reports this exact session inactive.
    state: str
    output: str = ""
    stopped: bool = False


class NativeExecutor:
    """Drive new jobs through the user's already-running local OpenCode service.

    This deliberately uses OpenCode's normal local account and permission model.
    It is not an OS credential sandbox: tools run with the same local access the
    signed-in OpenCode service already has. Fleet never reads or copies auth data.
    """

    _REQUEST_TIMEOUT = 20
    _JOB_TIMEOUT = 12 * 60 * 60
    _STOP_TIMEOUT = 60
    _POLL_SECONDS = 2

    def _call(self, config, method, path, body=None, timeout=None):
        command = [config.executable, "api"]
        if body is not None:
            command.extend(["--data", json.dumps(body, separators=(",", ":"))])
        command.extend([method, path])
        try:
            result = subprocess.run(
                command, cwd=config.cwd, capture_output=True, text=True,
                timeout=timeout or self._REQUEST_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AdmissionRefused(
                f"OpenCode API request could not complete ({method} {path})") from error
        if result.returncode != 0:
            match = re.search(r"HTTP (\d{3})", result.stdout + result.stderr)
            status = int(match.group(1)) if match else None
            detail = f"HTTP {status}" if status else "OpenCode API request failed"
            raise OpenCodeApiError(f"{detail} ({method} {path})", status=status)
        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise AdmissionRefused(
                f"OpenCode API returned invalid JSON ({method} {path})") from error

    @staticmethod
    def _data(response):
        if not isinstance(response, dict) or "data" not in response:
            raise AdmissionRefused("OpenCode API response omitted its data envelope")
        return response["data"]

    def verify(self, config):
        executable = Path(config.executable)
        if (not executable.is_file() or not os.access(executable, os.X_OK)
                or str(executable.resolve()) != config.executable):
            raise AdmissionRefused("Configured OpenCode executable is missing or changed")
        try:
            models = subprocess.run(
                [config.executable, "models"], cwd=config.cwd,
                capture_output=True, text=True, timeout=self._REQUEST_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AdmissionRefused("OpenCode model list could not be read") from error
        if models.returncode != 0 or not any(
                line.strip() == config.model for line in models.stdout.splitlines()):
            raise AdmissionRefused("Exact configured OpenCode model is not listed locally")
        providers = self._data(self._call(config, "GET", "/api/provider"))
        if not isinstance(providers, list) or not any(
                provider.get("id") == config.account
                and provider.get("integrationID") == config.account
                for provider in providers if isinstance(provider, dict)):
            raise AdmissionRefused(
                "Configured provider is not available in the existing OpenCode service")
        return True

    def verify_clean_start(self, config):
        sessions = self._list_sessions(config, search="Fleet-disposable")
        leftovers = [item for item in sessions if isinstance(item, dict)
                     and isinstance(item.get("title"), str)
                     and item["title"].startswith("Fleet-disposable:")]
        if leftovers:
            raise AdmissionRefused(
                "Unretired Fleet-disposable sessions need operator review before restart")
        return True

    def _list_sessions(self, config, **filters):
        params = {"limit": "100", "order": "desc", **filters}
        result = []
        seen = set()
        while True:
            response = self._call(config, "GET", "/api/session?" + urlencode(params))
            data = self._data(response)
            if not isinstance(data, list):
                raise AdmissionRefused("OpenCode session-list response is malformed")
            result.extend(data)
            cursor = response.get("cursor", {})
            next_cursor = cursor.get("next") if isinstance(cursor, dict) else None
            if not next_cursor:
                return result
            if next_cursor in seen:
                raise AdmissionRefused("OpenCode session-list cursor repeated")
            seen.add(next_cursor)
            params["cursor"] = next_cursor

    def _session_tree(self, config, session_id):
        result = [session_id]
        pending = [session_id]
        seen = {session_id}
        while pending:
            parent = pending.pop()
            for child in self._list_sessions(config, parentID=parent):
                child_id = child.get("id") if isinstance(child, dict) else None
                if isinstance(child_id, str) and _SESSION.fullmatch(child_id):
                    if child_id not in seen:
                        seen.add(child_id)
                        result.append(child_id)
                        pending.append(child_id)
        return result

    @staticmethod
    def job_cwd(config, job):
        root = Path(config.cwd)
        path = Path(job.cwd or config.cwd)
        if str(path.resolve()) != str(path) or not path.is_relative_to(root):
            raise AdmissionRefused("Job worktree must be inside the configured worktree root")
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False)
        if result.returncode != 0 or str(Path(result.stdout.strip()).resolve()) != str(path):
            raise AdmissionRefused("Each job must name an existing Git worktree root")
        return str(path)

    @staticmethod
    def _session_path(session_id, suffix=""):
        if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
            raise AdmissionRefused("OpenCode returned an invalid native session ID")
        return f"/api/session/{quote(session_id, safe='')}{suffix}"

    def create(self, config, job):
        cwd = self.job_cwd(config, job)
        provider_id, model_id = config.model.split("/", 1)
        response = self._call(config, "POST", "/api/session", {
            "title": f"Fleet-disposable:{job.id}",
            "location": {"directory": cwd},
            "model": {"providerID": provider_id, "id": model_id},
        })
        data = self._data(response)
        session_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
            raise AdmissionRefused("OpenCode did not return a fresh native session ID")
        try:
            session = self._data(self._call(
                config, "GET", self._session_path(session_id)))
            model = session.get("model") if isinstance(session, dict) else None
            location = session.get("directory") if isinstance(session, dict) else None
            if location is None and isinstance(session, dict):
                location = session.get("location", {}).get("directory")
            if (not isinstance(model, dict)
                    or model.get("providerID") != provider_id
                    or model.get("id") != model_id
                    or not isinstance(location, str)
                    or str(Path(location).resolve()) != cwd):
                raise AdmissionRefused(
                    "Created native session did not retain its exact worktree and model")
        except Exception as error:
            try:
                self.retire(config, session_id)
            except Exception as cleanup_error:
                raise NativeCreateError(
                    f"Session identity verification failed and retirement is unconfirmed: {cleanup_error}",
                    session_id=session_id, retired=False) from error
            raise NativeCreateError(
                "Session identity verification failed; the new session was retired",
                session_id=session_id, retired=True) from error
        return session_id

    def _active(self, config):
        data = self._data(self._call(config, "GET", "/api/session/active"))
        if not isinstance(data, dict):
            raise AdmissionRefused("OpenCode active-session response is malformed")
        return data

    def _active_ids(self, config):
        return set(self._active(config))

    def _messages(self, config, session_id):
        response = self._call(
            config, "GET", self._session_path(session_id, "/message?limit=50&order=desc"))
        data = self._data(response)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("messages", "items", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
        raise AdmissionRefused("OpenCode message response is malformed")

    def _permissions_pending(self, config, session_id):
        data = self._data(self._call(
            config, "GET", self._session_path(session_id, "/permission")))
        return isinstance(data, list) and bool(data)

    @staticmethod
    def _latest_assistant(messages):
        for item in messages:
            info = item.get("info", item) if isinstance(item, dict) else {}
            if (isinstance(info, dict)
                    and (info.get("role") == "assistant"
                         or info.get("type") == "assistant")):
                parts = item.get("parts", item.get("content", []))
                text = "".join(
                    part.get("text", "") for part in parts
                    if isinstance(part, dict) and part.get("type") == "text")
                return info, text
        return {}, ""

    def _interrupt(self, config, session_id):
        self._call(config, "POST", self._session_path(session_id, "/interrupt"))

    def _interrupt_tree(self, config, session_id):
        active = self._active_ids(config)
        for item in self._session_tree(config, session_id):
            if item in active:
                self._interrupt(config, item)

    def run(self, config, session_id, job, cancel):
        self._call(config, "POST", self._session_path(session_id, "/prompt"),
                   {"text": job.message})
        deadline = time.monotonic() + self._JOB_TIMEOUT
        interrupted = False
        cancelled = False
        stop_deadline = None
        while True:
            if cancel.is_set() and not interrupted:
                self._interrupt_tree(config, session_id)
                interrupted = True
                cancelled = True
                stop_deadline = time.monotonic() + self._STOP_TIMEOUT
            tree = self._session_tree(config, session_id)
            active = bool(set(tree) & self._active_ids(config))
            messages = self._messages(config, session_id)
            info, text = self._latest_assistant(messages)
            if not active and interrupted:
                state = "cancelled" if cancelled else "failed"
                output = text or ("Cancelled by operator" if cancelled else "Job timed out")
                return RunResult(state, output, stopped=True)
            if not active and info.get("error"):
                return RunResult("failed", text or str(info.get("error")), stopped=True)
            if not active and info.get("finish") == "stop":
                return RunResult("finished", text, stopped=True)
            if not active and self._permissions_pending(config, session_id):
                pass  # Normal OpenCode permission flow remains under user control.
            elif not active and info.get("finish") == "length":
                return RunResult("finished", text, stopped=True)
            elif not active and info.get("finish") == "error":
                return RunResult("failed", text, stopped=True)
            if not interrupted and time.monotonic() >= deadline:
                self._interrupt_tree(config, session_id)
                interrupted = True
                stop_deadline = time.monotonic() + self._STOP_TIMEOUT
            if interrupted and stop_deadline is not None and time.monotonic() >= stop_deadline:
                return RunResult("failed", text, stopped=False)
            cancel.wait(self._POLL_SECONDS)

    def retire(self, config, session_id):
        tree = self._session_tree(config, session_id)
        active = self._active_ids(config)
        if set(tree) & active:
            raise AdmissionRefused("Refusing to delete an active OpenCode session")
        self._call(config, "DELETE", self._session_path(session_id))
        for item in tree:
            try:
                self._call(config, "GET", self._session_path(item))
            except OpenCodeApiError as error:
                if error.status == 404:
                    continue
                raise
            raise AdmissionRefused("OpenCode session still exists after deletion")
        if set(tree) & self._active_ids(config):
            raise AdmissionRefused("OpenCode session tree remained active after deletion")
        return True


class OpenCodeApiError(AdmissionRefused):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NativeCreateError(AdmissionRefused):
    def __init__(self, message, session_id, retired):
        super().__init__(message)
        self.session_id = session_id
        self.retired = retired


class Fleet:
    """One lifetime, one queue, one native session per admitted new job.

    Executor contract: verify(config) authenticates exact dispatch bindings;
    create(config, job) returns a newly created native ID (never adopts one);
    run(config, sid, job, cancel_event) returns a RunResult with stopped proof;
    retire(config, sid) deletes that exact session and returns True only after
    verifying absence. A create exception is ambiguous, so all admission stops.
    Exceptions from run are unverified execution, so deletion is unsafe and
    admission stops. There is no retry, import, resume, or recovery operation.
    """
    def __init__(self, config, executor):
        if not isinstance(config, Config):
            raise AdmissionRefused("Explicit validated disposable configuration required")
        Config.parse({"executable": config.executable, "account": config.account,
                      "cwd": config.cwd, "model": config.model,
                      "maxConcurrent": config.max_concurrent})
        self.config = config
        self.executor = executor
        self._lock = threading.RLock()
        self._jobs = {}
        self._waiting = deque()
        self._active = {}
        self._used_sessions = set()
        self._terminal = {}
        self._threads = []
        self._blocked = None
        self._closed = False
        self._accepting = True

    def enqueue(self, raw):
        job = Job.parse(raw)
        with self._lock:
            if job.id in self._jobs:
                if self._jobs[job.id] != job:
                    raise AdmissionRefused("Job ID already belongs to different content")
                return {"id": job.id, "duplicate": True}
            if not self._accepting or self._closed or self._blocked:
                raise AdmissionRefused(self._blocked or "Fleet closed")
            worktree = None
            if hasattr(self.executor, "job_cwd"):
                worktree = self.executor.job_cwd(self.config, job)
                in_flight = set(self._waiting) | set(self._active)
                if any(self.executor.job_cwd(self.config, self._jobs[item]) == worktree
                       for item in in_flight):
                    raise AdmissionRefused("A worktree can have only one active or queued job")
            self.executor.verify(self.config)
            if len(self._waiting) >= MAX_QUEUED:
                raise AdmissionRefused("Disposable queue is full")
            self._jobs[job.id] = job
            self._waiting.append(job.id)
            self._admit()
            return {"id": job.id, "queued": True}

    def _admit(self):
        # Reservation occurs under the same lock as cancellation and terminal
        # publication. Retirement failures retain their slot and close admission.
        while (self._waiting and not self._closed and not self._blocked
               and len(self._active) < self.config.max_concurrent):
            identifier = self._waiting.popleft()
            cancel = threading.Event()
            self._active[identifier] = cancel
            worker = threading.Thread(target=self._run, args=(identifier, cancel))
            self._threads.append(worker)
            worker.start()

    def _finish(self, identifier, state, output="", session=None,
                retired=False, error=None, release=False):
        with self._lock:
            if identifier in self._terminal:
                raise RuntimeError("Terminal result already published")
            if (release and identifier in self._active
                    and self._active[identifier].is_set()):
                state = "cancelled"
            self._terminal[identifier] = {"id": identifier, "state": state,
                                          "output": output, "sessionId": session,
                                          "retired": retired, "error": error}
            if release:
                self._active.pop(identifier, None)
            else:
                self._blocked = error or "Unverified native session retirement"
            if self._blocked:
                # New jobs not yet admitted have no native effects to retire.
                # Give each its one terminal refusal so EOF draining converges.
                for waiting in self._waiting:
                    self._terminal[waiting] = {"id": waiting, "state": "failed",
                                               "output": "", "sessionId": None,
                                               "retired": False,
                                               "error": "Admission stopped: " + self._blocked}
                self._waiting.clear()
            self._admit()

    def _run(self, identifier, cancel):
        job = self._jobs[identifier]
        sid = None
        dispatch_error = None
        try:
            if cancel.is_set():
                self._finish(identifier, "cancelled", release=True)
                return
            self.executor.verify(self.config)
            sid = self.executor.create(self.config, job)
            with self._lock:
                if (not isinstance(sid, str) or not _SESSION.fullmatch(sid)
                        or sid in self._used_sessions):
                    raise AdmissionRefused("Native create returned invalid or reused session identity")
                self._used_sessions.add(sid)
        except Exception as error:
            sid = getattr(error, "session_id", sid)
            retired = getattr(error, "retired", False) is True
            self._finish(identifier, "failed", session=sid, retired=retired,
                         error=str(error), release=retired)
            return

        try:
            # A cancellation during create still owns the returned session; it
            # never sends a prompt and must retire it before releasing the slot.
            if cancel.is_set():
                result = RunResult("cancelled", stopped=True)
            else:
                try:
                    self.executor.verify(self.config)
                except Exception as error:
                    # No run call was made. Retirement must still clean up the
                    # session we just created even if admission proof expired.
                    dispatch_error = str(error)
                    with self._lock:
                        self._blocked = dispatch_error
                    result = RunResult("failed", stopped=True)
                else:
                    result = self.executor.run(self.config, sid, job, cancel)
            if (not isinstance(result, RunResult) or result.stopped is not True
                    or result.state not in {"finished", "failed", "cancelled"}):
                raise AdmissionRefused("Native execution stop/completion is unverified")
        except Exception as error:
            self._finish(identifier, "failed", session=sid, error=str(error))
            return

        try:
            if self.executor.retire(self.config, sid) is not True:
                raise AdmissionRefused("Native retirement was not confirmed")
        except Exception as error:
            self._finish(identifier, "failed", output=result.output, session=sid,
                         error="Session retirement failed: " + str(error))
            return
        # The lifecycle owner decides once, after a confirmed stop and deletion.
        state = "cancelled" if cancel.is_set() else result.state
        self._finish(identifier, state, result.output, sid, retired=True,
                     error=dispatch_error, release=True)

    def cancel(self, identifier):
        with self._lock:
            if identifier in self._terminal:
                return {"id": identifier, "terminal": True}
            if identifier in self._waiting:
                self._waiting.remove(identifier)
                self._finish(identifier, "cancelled", release=True)
            elif identifier in self._active:
                self._active[identifier].set()
            else:
                raise AdmissionRefused("Unknown new job")
            return {"id": identifier, "cancelRequested": True}

    def status(self):
        with self._lock:
            return {"model": self.config.model, "maxConcurrent": self.config.max_concurrent,
                    "queued": list(self._waiting), "active": list(self._active),
                    "blocked": self._blocked, "closed": self._closed,
                    "results": [dict(item) for item in self._terminal.values()]}

    def close(self):
        """Cancel pending/active jobs, drain owned execution and retire once."""
        with self._lock:
            self._closed = True
            self._accepting = False
            for identifier in list(self._waiting) + list(self._active):
                self.cancel(identifier)
            threads = list(self._threads)
        for worker in threads:
            worker.join()

    def drain(self):
        """Close input and finish accepted work, including jobs awaiting a slot."""
        with self._lock:
            self._accepting = False
        while True:
            with self._lock:
                threads = list(self._threads)
            for worker in threads:
                worker.join()
            with self._lock:
                if len(threads) == len(self._threads):
                    self._closed = True
                    return


def command(config_path, stream, output, inspect=False):
    """Supported JSON-lines command; all queue/results vanish on process exit.

    EOF drains jobs; explicit cancellation/interrupt cancels them. No file queue
    is ever loaded. Configuration
    deliberately has no default executable, account, model or cap.
    """
    raw = json.loads(Path(config_path).read_text())
    config = Config.parse(raw)
    executor = NativeExecutor()
    if inspect:
        try:
            executor.verify(config)
            executor.verify_clean_start(config)
        except AdmissionRefused as error:
            output({"ready": False, "reason": str(error), "dispatch": asdict(config)})
            return
        output({"ready": True, "dispatch": asdict(config)})
        return
    executor.verify(config)  # Read-only local capability check; no quota probe.
    lock_fd = _acquire_manager_lock(config)
    try:
        executor.verify(config)
        executor.verify_clean_start(config)
        fleet = Fleet(config, executor)
        try:
            for line in stream:
                try:
                    request = json.loads(line)
                    if set(request) == {"enqueue"}:
                        response = fleet.enqueue(request["enqueue"])
                    elif set(request) == {"cancel"}:
                        response = fleet.cancel(request["cancel"])
                    elif request == {"status": True}:
                        response = fleet.status()
                    elif request == {"stop": True}:
                        output({"stopping": True})
                        fleet.close()
                        break
                    else:
                        raise AdmissionRefused("Only enqueue/cancel/status/stop supported")
                    output(response)
                except (ValueError, TypeError, AdmissionRefused) as error:
                    output({"refused": str(error)})
            fleet.drain()
        except BaseException:
            fleet.close()
            raise
        finally:
            snapshot = fleet.status()
            for terminal in snapshot.pop("results"):
                output({"terminal": terminal})
            output(snapshot)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _acquire_manager_lock(config):
    """Keep the three-job cap global across concurrent manager invocations."""
    account_digest = hashlib.sha256(config.account.encode()).hexdigest()[:16]
    directory = Path.home() / "Developer" / ".codex" / "fleet"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"disposable-opencode-{account_digest}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(fd)
        raise AdmissionRefused(
            "A disposable OpenCode manager already owns this account") from error
    return fd
