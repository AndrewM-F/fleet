"""Process-local new-job queue. No roster, ledger, recovery or session reuse.

The executor owns native I/O and must prove account/eligibility/isolation before
every create and productive call. There is deliberately no configurable plugin
loader or command override: an enforcing native executor must be source reviewed.
"""
from collections import deque
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import threading

from . import credential_isolation

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

    @classmethod
    def parse(cls, raw):
        if (not isinstance(raw, dict) or set(raw) != {"id", "message", "rootAuthorized"}
                or raw["rootAuthorized"] is not True
                or not isinstance(raw["id"], str) or not _ID.fullmatch(raw["id"])
                or not isinstance(raw["message"], str) or not raw["message"].strip()
                or len(raw["message"].encode()) > MAX_MESSAGE_BYTES):
            raise AdmissionRefused("New root-authorized id/message only; session/recovery fields refused")
        return cls(raw["id"], raw["message"])


@dataclass(frozen=True)
class RunResult:
    # stopped is a backend proof, not merely a CLI exit code. Descendants must
    # be gone before deletion and admission of another native session.
    state: str
    output: str = ""
    stopped: bool = False


class NativeExecutor:
    """Fail closed until a supported enforcing native driver is established.

    No Popen fallback: the existing launch gate must not be bypassed to make
    disposable sessions appear operational. Status never invokes OpenCode,
    reads credentials, creates a session, or probes quota.
    """
    def verify(self, config):
        isolation = credential_isolation.status()
        gaps = ["Current signed-in account and model quota eligibility unverified",
                "Disposable native create/run/delete driver not qualified"]
        if not isolation["ready"]:
            gaps.append(isolation["reason"])
        raise AdmissionRefused("; ".join(gaps))


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
            self._finish(identifier, "failed", session=sid, error=str(error))
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
        except AdmissionRefused as error:
            output({"ready": False, "reason": str(error), "dispatch": asdict(config)})
            return
        output({"ready": True, "dispatch": asdict(config)})
        return
    executor.verify(config)  # Refuse before accepting any job or native effect.
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
                else:
                    raise AdmissionRefused("Only enqueue/cancel/status supported")
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
