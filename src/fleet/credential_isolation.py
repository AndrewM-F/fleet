"""Versioned launcher trust contract. No enforcing production backend is shipped.

This module does not sandbox a process. Only a separately reviewed, statically
registered enforcing backend may attest and launch one. Worker JSON, environment
variables and command filters never establish that trust.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import time
from types import MappingProxyType

from . import paths

CONTRACT_VERSION = 1
POLICY_VERSION = 1
MAX_EVIDENCE_AGE_SECONDS = 30
REQUIRED_DENIALS = frozenset(
    {
        "host-credential-stores",
        "keychain",
        "credential-helper-ipc",
        "inherited-credential-descriptors",
        "credential-environment",
        "descendant-escape",
    }
)
# Source-reviewed registration only. Configuration cannot import or register code.
# Intentionally empty: no supported enforcing backend has been established.
_BACKENDS = MappingProxyType({})


class IsolationUnavailable(RuntimeError):
    """The trusted enforcing-launcher contract could not be proved."""


class IsolationLaunchUncertain(IsolationUnavailable):
    """A backend may have spawned; cleanup requires explicit reconciliation."""


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _canonical(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise IsolationUnavailable("Isolation paths must be absolute canonical paths")
    canonical = str(Path(value).resolve())
    if value != canonical:
        raise IsolationUnavailable(
            "Isolation paths must be canonical; aliases are refused"
        )
    return canonical


@dataclass(frozen=True)
class Binding:
    contract_version: int
    backend_id: str
    backend_version: str
    policy_version: int
    policy_digest: str
    executable: str
    executable_sha256: str
    executable_version: str
    cwd: str
    session_cwd: str
    database_path: str
    turn_root: str
    log_dir: str
    allowed_paths: tuple
    denied_resources: tuple
    session_id: str
    task_id: str
    turn_id: str
    launch_instance: str
    model: str

    @property
    def digest(self):
        return _digest(asdict(self))


@dataclass(frozen=True)
class Evidence:
    """Proof returned over a backend-owned channel, never native worker stdout.

    The backend verifier must authenticate proof against its own trust anchor
    and measured enforcing launcher. Matching these fields alone is not proof.
    """

    contract_version: int
    backend_id: str
    backend_version: str
    policy_digest: str
    binding_digest: str
    stage: str
    issued_at: float
    expires_at: float
    process_id: int | None
    proof: bytes


def status():
    """Read-only capability report; never inspect a credential or start a backend."""
    return {
        "contractVersion": CONTRACT_VERSION,
        "policyVersion": POLICY_VERSION,
        "ready": False,
        "supportedBackends": sorted(_BACKENDS),
        "reason": "No production enforcing backend has been established",
    }


def _binding(request):
    try:
        policy = paths.config().get("credentialIsolation")
        if not isinstance(policy, dict):
            raise IsolationUnavailable(
                "Credential isolation policy is absent; launch refused"
            )
        if (
            type(policy.get("contractVersion")) is not int
            or policy["contractVersion"] != CONTRACT_VERSION
            or type(policy.get("policyVersion")) is not int
            or policy["policyVersion"] != POLICY_VERSION
        ):
            raise IsolationUnavailable("Unknown isolation contract or policy version")
        backend_id = policy["backendId"]
        backend = _BACKENDS.get(backend_id)
        if backend is None:
            raise IsolationUnavailable(
                "No supported enforcing isolation backend; launch refused"
            )
        if policy["backendVersion"] != backend.version:
            raise IsolationUnavailable("Enforcing backend version mismatch")
        body = {k: v for k, v in policy.items() if k != "policyDigest"}
        if policy["policyDigest"] != _digest(body):
            raise IsolationUnavailable("Isolation policy digest mismatch")
        allowed = tuple(sorted({_canonical(p) for p in policy["allowedPaths"]}))
        denied = tuple(sorted(set(policy["deniedResources"])))
        if not REQUIRED_DENIALS.issubset(denied):
            raise IsolationUnavailable(
                "Isolation policy omits a required credential boundary"
            )
        executable = _canonical(request["executable"])
        executable_sha = hashlib.sha256(Path(executable).read_bytes()).hexdigest()
        if executable_sha != policy["executableSha256"]:
            raise IsolationUnavailable("Isolation executable digest mismatch")
        version = policy["executableVersion"]
        if not isinstance(version, str) or not version:
            raise IsolationUnavailable("Isolation executable version is absent")
        cwd = _canonical(request["cwd"])
        session_cwd = _canonical(request["sessionCwd"])
        database = _canonical(request["databasePath"])
        if cwd != session_cwd or not {cwd, database, executable}.issubset(allowed):
            raise IsolationUnavailable("Isolation canonical path allowlist mismatch")
        turn_root = _canonical(request["turnRoot"])
        log_dir = _canonical(request["logDir"])
        if not Path(turn_root).is_relative_to(
            _canonical(policy["runtimeRoot"])
        ) or not Path(log_dir).is_relative_to(_canonical(policy["logRoot"])):
            raise IsolationUnavailable("Isolation runtime/log path mismatch")
        identifiers = [
            request[k]
            for k in ("sessionId", "taskId", "turn", "isolationLaunchId", "model")
        ]
        if any(not isinstance(v, str) or not v.strip() for v in identifiers):
            raise IsolationUnavailable("Isolation launch identity is incomplete")
        binding = Binding(
            CONTRACT_VERSION,
            backend_id,
            backend.version,
            POLICY_VERSION,
            policy["policyDigest"],
            executable,
            executable_sha,
            version,
            cwd,
            session_cwd,
            database,
            turn_root,
            log_dir,
            allowed,
            denied,
            *identifiers,
        )
        return backend, binding
    except IsolationUnavailable:
        raise
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise IsolationUnavailable(
            "Isolation policy/launch identity is unreadable or invalid"
        ) from error


def _verify(backend, binding, evidence, stage, process_id=None):
    now = time.time()
    if (
        not isinstance(evidence, Evidence)
        or type(evidence.contract_version) is not int
        or evidence.contract_version != CONTRACT_VERSION
        or evidence.backend_id != binding.backend_id
        or evidence.backend_version != binding.backend_version
        or evidence.policy_digest != binding.policy_digest
        or evidence.binding_digest != binding.digest
        or evidence.stage != stage
        or evidence.process_id != process_id
        or not isinstance(evidence.proof, bytes)
        or not evidence.proof
        or type(evidence.issued_at) not in (int, float)
        or type(evidence.expires_at) not in (int, float)
        or not (now - MAX_EVIDENCE_AGE_SECONDS <= evidence.issued_at <= now)
        or not (
            now < evidence.expires_at <= evidence.issued_at + MAX_EVIDENCE_AGE_SECONDS
        )
    ):
        raise IsolationUnavailable(
            "Enforcing launcher evidence is absent, stale or mismatched"
        )
    if backend.verify(evidence, binding, stage, process_id) is not True:
        raise IsolationUnavailable("Enforcing launcher proof was not authenticated")


def pre_prompt(request):
    """Gate prompt persistence using fresh backend-owned evidence for this launch."""
    backend, binding = _binding(request)
    _verify(backend, binding, backend.attest(binding, "pre-prompt"), "pre-prompt")


def launch(request, args, **kwargs):
    """Delegate process creation to the enforcing backend, with no fallback.

    A fresh launch attestation is required independently of the pre-prompt
    check. The backend must atomically enforce the bound policy when spawning
    and return authenticated evidence for the resulting process before Fleet
    writes any prompt bytes to its stdin.
    """
    backend, binding = _binding(request)
    _verify(backend, binding, backend.attest(binding, "launch"), "launch")
    if args[0] != binding.executable or kwargs.get("cwd") != binding.cwd:
        raise IsolationUnavailable("Launch command differs from the attested binding")
    try:
        process, evidence = backend.launch(binding, args, **kwargs)
    except Exception as error:
        raise IsolationLaunchUncertain(
            "Enforcing backend launch outcome is unknown; reconcile before retry"
        ) from error
    try:
        _verify(backend, binding, evidence, "process", process.pid)
    except Exception:
        # Backend contract also owns descendant containment and teardown. No
        # prompt has been sent. Do not continue with a partially proved child.
        try:
            cleanup_evidence = backend.abort(process, binding)
            _verify(backend, binding, cleanup_evidence, "cleanup", process.pid)
        except Exception as error:
            raise IsolationLaunchUncertain(
                "Enforcing backend cleanup is unverified; reconcile before retry"
            ) from error
        raise
    return process
