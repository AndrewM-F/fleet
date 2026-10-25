"""New-job queue; verified model continuation stays inside one owned session."""
from collections import deque
from dataclasses import asdict, dataclass, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote, urlencode

MODEL = "opencode/mimo-v2.6-flash-free"
PAID_MODEL = "opencode-go/mimo-v2.6-flash"
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
    fallback_model: str | None = None
    quota_evidence_path: str | None = None

    @classmethod
    def parse(cls, raw):
        required = {"executable", "account", "cwd", "model", "maxConcurrent"}
        optional = {"fallbackModel", "quotaEvidencePath"}
        if (not isinstance(raw, dict) or not required <= set(raw)
                or set(raw) - required - optional):
            raise AdmissionRefused("Exact disposable executable/account/cwd/model/cap required")
        if raw["model"] not in (MODEL, PAID_MODEL) or type(raw["maxConcurrent"]) is not int or not (
                1 <= raw["maxConcurrent"] <= MAX_CONCURRENT):
            raise AdmissionRefused("Only authorized MiMo Flash models and at most three jobs allowed")
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
        fallback = raw.get("fallbackModel")
        quota_path = raw.get("quotaEvidencePath")
        if fallback is not None and (fallback != PAID_MODEL or raw["model"] != MODEL):
            raise AdmissionRefused("Only free MiMo to authorized Go Flash fallback is allowed")
        if quota_path is not None and (
                not isinstance(quota_path, str) or not Path(quota_path).is_absolute()
                or str(Path(quota_path).resolve()) != quota_path):
            raise AdmissionRefused("Quota evidence path must be explicit and canonical")
        if (fallback or raw["model"] == PAID_MODEL) and not quota_path:
            raise AdmissionRefused("Paid selection requires separate verified quota evidence")
        return cls(raw["executable"], raw["account"], raw["cwd"], raw["model"],
                   raw["maxConcurrent"], fallback, quota_path)

    def raw(self):
        result = {"executable": self.executable, "account": self.account,
                  "cwd": self.cwd, "model": self.model, "maxConcurrent": self.max_concurrent}
        if self.fallback_model is not None:
            result["fallbackModel"] = self.fallback_model
        if self.quota_evidence_path is not None:
            result["quotaEvidencePath"] = self.quota_evidence_path
        return result


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
    preserve_session: bool = False
    error: str | None = None
    transitions: tuple = ()


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
    _MAX_RESPONSE_BYTES = 16 * 1024 * 1024
    _MAX_MESSAGE_PAGES = 100

    def __init__(self):
        self._quota_lock = threading.RLock()

    def _quota(self, config):
        if not config.quota_evidence_path:
            return {"version": 1, "holds": {}, "signatures": []}
        path = Path(config.quota_evidence_path)
        try:
            state = json.loads(path.read_text()) if path.exists() else {
                "version": 1, "holds": {}, "signatures": []}
            if (not isinstance(state, dict) or type(state.get("version")) is not int
                    or state["version"] != 1
                    or set(state) - {"version", "holds", "signatures", "paidFailure"}
                    or not isinstance(state.get("holds"), dict)
                    or not isinstance(state.get("signatures"), list)):
                raise ValueError("schema")
            for model, proof in state["holds"].items():
                self._proof(model, proof)
            for signature in state["signatures"]:
                self._proof(signature.get("model"), signature)
                if (not isinstance(signature.get("type"), str) or not signature["type"]
                        or type(signature.get("status")) is not int
                        or signature["status"] not in (402, 429)
                        or not isinstance(signature.get("messagePrefix"), str)
                        or not 12 <= len(signature["messagePrefix"]) <= 512):
                    raise ValueError("signature")
            if "paidFailure" in state and not isinstance(state["paidFailure"], dict):
                raise ValueError("paid failure")
            return state
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise AdmissionRefused("Quota evidence is unreadable or unverified; no dispatch") from error

    @staticmethod
    def _proof(model, proof):
        if (model not in (MODEL, PAID_MODEL) or not isinstance(proof, dict)
                or proof.get("verified") is not True
                or not isinstance(proof.get("evidence"), str)
                or not 12 <= len(proof["evidence"]) <= 4096):
            raise ValueError("verified quota proof required")

    def _quota_failure(self, config, model, info):
        """Only a root-verified exact native error signature establishes exhaustion.

        Native v2 errors have no quota enum. Generic 429s, auth and network errors
        are deliberately not sufficient, and no provider signature is guessed.
        """
        error = info.get("error")
        if not isinstance(error, dict):
            return None
        state = self._quota(config)
        for signature in state["signatures"]:
            if (signature["model"] == model and error.get("type") == signature["type"]
                    and error.get("status") == signature["status"]
                    and isinstance(error.get("message"), str)
                    and error["message"].startswith(signature["messagePrefix"])):
                return signature
        return None

    def _record_quota(self, config, model, info, proof=None, reason="native-error"):
        if not config.quota_evidence_path:
            return
        with self._quota_lock:
            state = self._quota(config)
            if proof is not None:
                state["holds"][model] = {
                    "verified": True, "evidence": proof["evidence"],
                    "assistantID": info.get("id"), "source": "matched-verified-native-signature"}
            elif model == PAID_MODEL:
                # Preserve a paid failure separately; do not mislabel it verified quota.
                error = info.get("error", {})
                state["paidFailure"] = {"kind": reason, "assistantID": info.get("id"),
                                        "status": error.get("status") if isinstance(error, dict)
                                        and type(error.get("status")) is int else None,
                                        "source": "native-paid-failure;operator-review-required"}
            path = Path(config.quota_evidence_path)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".quota-", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as output:
                    json.dump(state, output, sort_keys=True)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def _switch_model(self, config, session_id, job, model):
        if set(self._session_tree(config, session_id)) & self._active_ids(config):
            raise AdmissionRefused("Cannot switch an active native session")
        provider, identifier = model.split("/", 1)
        self._call(config, "POST", self._session_path(session_id, "/model"),
                   {"model": {"providerID": provider, "id": identifier}})
        session = self._data(self._call(config, "GET", self._session_path(session_id)))
        if not isinstance(session, dict):
            raise AdmissionRefused("Native model-switch readback is malformed")
        location = session.get("directory") or session.get("location", {}).get("directory")
        if (session.get("id") != session_id or session.get("model") != {
                "providerID": provider, "id": identifier}
                or location != self.job_cwd(config, job)):
            raise AdmissionRefused("Native switch did not preserve session/worktree/exact model")

    def _call(self, config, method, path, body=None, timeout=None):
        command = [config.executable, "api"]
        if body is not None:
            command.extend(["--data", json.dumps(body, separators=(",", ":"))])
        command.extend([method, path])
        try:
            # The native CLI can exit before piped stdout finishes flushing.
            # A private regular file receives the full response before exit.
            # Never log response bodies; they can contain job or account data.
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                result = subprocess.run(
                    command, cwd=config.cwd, stdout=output, stderr=errors,
                    timeout=timeout or self._REQUEST_TIMEOUT, check=False)
                output.seek(0)
                payload = output.read(self._MAX_RESPONSE_BYTES + 1)
                errors.seek(0)
                diagnostic = errors.read(4096)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AdmissionRefused(
                f"OpenCode API request could not complete ({method} {path})") from error
        if result.returncode != 0:
            match = re.search(rb"HTTP (\d{3})", payload + diagnostic)
            status = int(match.group(1)) if match else None
            detail = f"HTTP {status}" if status else "OpenCode API request failed"
            raise OpenCodeApiError(f"{detail} ({method} {path})", status=status)
        if len(payload) > self._MAX_RESPONSE_BYTES:
            raise AdmissionRefused("OpenCode API response exceeded the bounded response size")
        if not payload.strip():
            return None
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise AdmissionRefused(
                f"OpenCode API returned invalid JSON ({method} {path})") from error

    @staticmethod
    def _data(response):
        if not isinstance(response, dict) or "data" not in response:
            raise AdmissionRefused("OpenCode API response omitted its data envelope")
        return response["data"]

    def verify(self, config):
        state = self._quota(config)
        paid_enabled = config.model == PAID_MODEL or config.fallback_model == PAID_MODEL
        if paid_enabled and (PAID_MODEL in state["holds"] or "paidFailure" in state):
            raise AdmissionRefused("Paid model is held after failure; operator review required")
        if config.model == PAID_MODEL and MODEL not in state["holds"]:
            raise AdmissionRefused("Direct paid admission requires a verified free-quota hold")
        if (config.model == MODEL and MODEL in state["holds"]
                and config.fallback_model != PAID_MODEL):
            raise AdmissionRefused("Free quota is held and no paid fallback is authorized")
        executable = Path(config.executable)
        if (not executable.is_file() or not os.access(executable, os.X_OK)
                or str(executable.resolve()) != config.executable):
            raise AdmissionRefused("Configured OpenCode executable is missing or changed")
        if config.account != config.model.split("/", 1)[0]:
            raise AdmissionRefused("Configured provider does not match the exact model")
        try:
            models = subprocess.run(
                [config.executable, "models"], cwd=config.cwd,
                capture_output=True, text=True, timeout=self._REQUEST_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AdmissionRefused("OpenCode model list could not be read") from error
        required_models = {config.model, *([config.fallback_model] if config.fallback_model else [])}
        if models.returncode != 0 or not required_models <= set(models.stdout.splitlines()):
            raise AdmissionRefused("Exact configured OpenCode model is not listed locally")
        providers = self._data(self._call(config, "GET", "/api/provider"))
        if not isinstance(providers, list) or not any(
                provider.get("id") == config.account
                and provider.get("integrationID") == (
                    "opencode" if config.model == PAID_MODEL else config.account)
                for provider in providers if isinstance(provider, dict)):
            raise AdmissionRefused(
                "Configured provider is not available in the existing OpenCode service")
        if config.fallback_model and not any(
                isinstance(provider, dict) and provider.get("id") == "opencode-go"
                and provider.get("integrationID") == "opencode" for provider in providers):
            raise AdmissionRefused("Existing OpenCode Go integration is unavailable")
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
        params = {"limit": "10", "order": "desc"}
        messages = []
        seen = set()
        for _ in range(self._MAX_MESSAGE_PAGES):
            response = self._call(config, "GET", self._session_path(
                session_id, "/message?" + urlencode(params)))
            data = self._data(response)
            if isinstance(data, dict):
                data = next((data[key] for key in ("messages", "items", "data")
                             if isinstance(data.get(key), list)), None)
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise AdmissionRefused("OpenCode message response is malformed")
            messages.extend(data)
            # Newest-first ordering is retained across cursor pages. Once an
            # assistant is found, older messages cannot change that selection.
            if self._latest_assistant(data)[0]:
                return messages
            cursor = response.get("cursor", {})
            if not isinstance(cursor, dict):
                raise AdmissionRefused("OpenCode message cursor is malformed")
            following = cursor.get("next")
            if following is None:
                return messages
            if not isinstance(following, str) or not following or len(following) > 4096:
                raise AdmissionRefused("OpenCode message cursor is malformed")
            if following in seen:
                raise AdmissionRefused("OpenCode message cursor repeated")
            seen.add(following)
            params = {"limit": "10", "cursor": following}
        raise AdmissionRefused("OpenCode message pagination exceeded its page bound")

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
                if "id" not in info and isinstance(item.get("id"), str):
                    info = {**info, "id": item["id"]}
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
        model = config.model
        transitions = []
        state = self._quota(config)
        if model == MODEL and MODEL in state["holds"]:
            if config.fallback_model != PAID_MODEL:
                raise AdmissionRefused("Verified free quota hold forbids another free request")
            model = PAID_MODEL
        if model == PAID_MODEL:
            paid = replace(config, model=PAID_MODEL, account="opencode-go", fallback_model=None)
            self.verify(paid)
            if cancel.is_set():
                return RunResult("cancelled", stopped=True)
            self._switch_model(config, session_id, job, model)
            transitions.append({"model": PAID_MODEL, "reason": "verified-free-quota-hold"})
        if cancel.is_set():
            return RunResult("cancelled", stopped=True, transitions=tuple(transitions))
        # Exactly one initial task prompt. Ambiguous API outcomes are never replayed.
        self._call(config, "POST", self._session_path(session_id, "/prompt"),
                   {"text": job.message})
        deadline = time.monotonic() + self._JOB_TIMEOUT
        interrupted = False
        cancelled = False
        stop_deadline = None
        previous_assistant = None
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
            if previous_assistant and info.get("id") == previous_assistant:
                info = {}  # Await the paid turn; the prior free error is not its result.
            if not active and interrupted:
                if not cancelled and model == PAID_MODEL:
                    return RunResult(
                        "failed", text or "Paid job timed out", stopped=True,
                        preserve_session=True,
                        error="Paid model timed out; no retry; native context preserved",
                        transitions=tuple(transitions))
                status = "cancelled" if cancelled else "failed"
                output = text or ("Cancelled by operator" if cancelled else "Job timed out")
                return RunResult(status, output, stopped=True, transitions=tuple(transitions))
            if not active and info.get("error"):
                proof = self._quota_failure(config, model, info)
                if proof or model == PAID_MODEL:
                    self._record_quota(config, model, info, proof)
                if model == PAID_MODEL:
                    return RunResult("failed", text, stopped=True, preserve_session=True,
                                     error="Paid model failed; no retry; native context preserved",
                                     transitions=tuple(transitions))
                state = self._quota(config)
                if (proof is not None and config.fallback_model == PAID_MODEL
                        and MODEL in state["holds"] and isinstance(info.get("id"), str) and info["id"].startswith("msg_")):
                    paid = replace(config, model=PAID_MODEL, account="opencode-go",
                                   fallback_model=None)
                    self.verify(paid)
                    if cancel.is_set():
                        return RunResult("cancelled", stopped=True)
                    self._switch_model(config, session_id, job, PAID_MODEL)
                    if cancel.is_set():
                        return RunResult("cancelled", stopped=True)
                    previous_assistant = info["id"]
                    continuation_id = "msg_fallback_" + hashlib.sha256(
                        f"{session_id}:{job.id}:{previous_assistant}".encode()).hexdigest()[:24]
                    # A continuation, not a resend of the original task or a fork.
                    self._call(config, "POST", self._session_path(session_id, "/prompt"), {
                        "id": continuation_id,
                        "text": "Continue the already approved task in this same session after "
                                "the verified free-quota stop. Inspect existing progress and "
                                "partial work first; do not repeat completed side effects."})
                    model = PAID_MODEL
                    transitions.append({"model": model, "reason": "verified-free-quota-error",
                                        "previousAssistantID": previous_assistant})
                    continue
                return RunResult("failed", text, stopped=True,
                                 preserve_session=bool(config.fallback_model),
                                 error="Free error has no verified quota fallback proof",
                                 transitions=tuple(transitions))
            if not active and info.get("finish") == "stop":
                return RunResult("finished", text, stopped=True, transitions=tuple(transitions))
            if not active and self._permissions_pending(config, session_id):
                pass  # Existing native permission controls are unchanged.
            elif not active and info.get("finish") == "length":
                return RunResult("finished", text, stopped=True, transitions=tuple(transitions))
            elif not active and info.get("finish") == "error":
                if model == PAID_MODEL:
                    self._record_quota(config, model, info)
                return RunResult("failed", text, stopped=True,
                                 preserve_session=bool(config.fallback_model) or model == PAID_MODEL,
                                 error="Unclassified native failure; no provider retry",
                                 transitions=tuple(transitions))
            if not interrupted and time.monotonic() >= deadline:
                # Classify once at the deadline decision. A cancellation already
                # requested after the loop-top check wins over timeout.
                cancelled = cancel.is_set()
                interrupted = True
                stop_deadline = time.monotonic() + self._STOP_TIMEOUT
                try:
                    if not cancelled and model == PAID_MODEL:
                        self._record_quota(config, model, info, reason="timeout")
                finally:
                    # Persist first, but still attempt a stop if evidence I/O fails.
                    self._interrupt_tree(config, session_id)
            if interrupted and stop_deadline is not None and time.monotonic() >= stop_deadline:
                return RunResult("failed", text, stopped=False,
                                 preserve_session=model == PAID_MODEL,
                                 transitions=tuple(transitions))
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
    admission stops. Verified free-quota continuation stays within the same job;
    there is no task replay, history import or old-session recovery operation.
    """
    def __init__(self, config, executor):
        if not isinstance(config, Config):
            raise AdmissionRefused("Explicit validated disposable configuration required")
        Config.parse(config.raw())
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
                retired=False, error=None, release=False, transitions=()):
        with self._lock:
            if identifier in self._terminal:
                raise RuntimeError("Terminal result already published")
            if (release and identifier in self._active
                    and self._active[identifier].is_set()):
                state = "cancelled"
            self._terminal[identifier] = {"id": identifier, "state": state,
                                          "output": output, "sessionId": session,
                                          "retired": retired, "error": error}
            if transitions:
                self._terminal[identifier]["providerTransitions"] = list(transitions)
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

        if result.preserve_session:
            self._finish(identifier, result.state, result.output, sid, retired=False,
                         error=result.error or "Native session preserved for operator review",
                         transitions=result.transitions)
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
                     error=dispatch_error or result.error, release=True,
                     transitions=result.transitions)

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
    binding = "opencode" if config.account in {"opencode", "opencode-go"} else config.account
    account_digest = hashlib.sha256(binding.encode()).hexdigest()[:16]
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
