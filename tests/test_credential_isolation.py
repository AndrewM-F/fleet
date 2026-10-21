"""Contract tests only: no real backend, credential store or OpenCode probe."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import credential_isolation as isolation, opencode, paths  # noqa: E402


class FakeBackend:
    """In-process fake authority, not a confinement implementation."""

    version = "fake-v1"

    def __init__(self):
        self.spawned = []
        self.aborted = []
        self.change_evidence = lambda evidence: evidence

    def attest(self, binding, stage, process_id=None):
        now = time.time()
        return self.change_evidence(
            isolation.Evidence(
                1,
                binding.backend_id,
                self.version,
                binding.policy_digest,
                binding.digest,
                stage,
                now - 0.01,
                now + 10,
                process_id,
                b"fake-proof",
            )
        )

    def verify(self, evidence, binding, stage, process_id):
        return evidence.proof == b"fake-proof"

    def launch(self, binding, args, **kwargs):
        self.spawned.append((binding, args, kwargs))
        process = SimpleNamespace(pid=12345)
        return process, self.attest(binding, "process", process.pid)

    def abort(self, process, binding):
        self.aborted.append(process.pid)
        return self.attest(binding, "cleanup", process.pid)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        executable = self.root / "fake-executable"
        executable.write_bytes(b"synthetic executable never run")
        self.request = {
            "executable": str(executable),
            "cwd": str(self.root),
            "sessionCwd": str(self.root),
            "databasePath": str(self.root / "fake.db"),
            "sessionId": "ses_TEST",
            "taskId": "task",
            "turn": "turn",
            "isolationLaunchId": "launch-instance",
            "model": opencode.MODEL,
        }
        self.request.update(
            turnRoot=str(self.root / "turn"), logDir=str(self.root / "logs")
        )
        self.policy = {
            "contractVersion": 1,
            "policyVersion": 1,
            "backendId": "fake",
            "backendVersion": "fake-v1",
            "executableVersion": "fake-executable-v1",
            "executableSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "allowedPaths": sorted(
                {str(self.root), str(executable), self.request["databasePath"]}
            ),
            "deniedResources": sorted(isolation.REQUIRED_DENIALS),
        }
        self.policy.update(runtimeRoot=str(self.root), logRoot=str(self.root))
        self.rehash()
        self.backend = FakeBackend()
        for name, value in (("_BACKENDS", {"fake": self.backend}),):
            gate = patch.object(isolation, name, value)
            gate.start()
            self.addCleanup(gate.stop)
        cfg = patch.object(
            paths, "config", side_effect=lambda: {"credentialIsolation": self.policy}
        )
        cfg.start()
        self.addCleanup(cfg.stop)

    def rehash(self):
        self.policy["policyDigest"] = isolation._digest(
            {k: v for k, v in self.policy.items() if k != "policyDigest"}
        )

    def launch(self):
        return isolation.launch(
            self.request, [self.request["executable"], "fake"], cwd=self.request["cwd"]
        )

    def test_fresh_backend_evidence_allows_only_the_bound_fake_launch(self):
        isolation.pre_prompt(self.request)
        self.assertEqual(self.launch().pid, 12345)
        self.assertEqual(len(self.backend.spawned), 1)

    def test_absent_unknown_or_mismatched_contract_refuses(self):
        cases = (
            {},
            {**self.policy, "backendId": "unknown"},
            {**self.policy, "contractVersion": 99},
            {**self.policy, "policyVersion": 99},
            {**self.policy, "backendVersion": "stale"},
            {**self.policy, "policyDigest": "wrong"},
        )
        for policy in cases:
            with (
                self.subTest(policy=policy),
                patch.object(
                    paths, "config", return_value={"credentialIsolation": policy}
                ),
            ):
                with self.assertRaises(isolation.IsolationUnavailable):
                    isolation.pre_prompt(self.request)
                with self.assertRaises(isolation.IsolationUnavailable):
                    self.launch()
        self.assertEqual(self.backend.spawned, [])

    def test_worker_json_boolean_cannot_supply_trusted_proof(self):
        self.request["credentialIsolation"] = {"enforced": True, "verified": True}
        with patch.object(isolation, "_BACKENDS", {}):
            with self.assertRaises(isolation.IsolationUnavailable):
                isolation.pre_prompt(self.request)
        self.backend.change_evidence = lambda evidence: {"enforced": True}
        with self.assertRaises(isolation.IsolationUnavailable):
            isolation.pre_prompt(self.request)

    def test_stale_future_mismatched_or_forged_evidence_refuses_before_spawn(self):
        mutations = (
            {"issued_at": time.time() - 60},
            {"issued_at": time.time() + 60},
            {"expires_at": time.time() - 1},
            {"expires_at": time.time() + 3600},
            {"binding_digest": "foreign-launch"},
            {"policy_digest": "foreign-policy"},
            {"backend_version": "old"},
            {"stage": "pre-prompt"},
            {"process_id": 999},
            {"proof": b"forged"},
        )
        for mutation in mutations:
            self.backend.change_evidence = lambda evidence, m=mutation: replace(
                evidence, **m
            )
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(isolation.IsolationUnavailable),
            ):
                self.launch()
        self.assertEqual(self.backend.spawned, [])

    def test_binding_changes_for_every_identity_and_path_component(self):
        _, before = isolation._binding(self.request)
        for field in ("sessionId", "taskId", "turn", "isolationLaunchId", "model"):
            with self.subTest(field=field):
                _, after = isolation._binding({**self.request, field: "changed"})
                self.assertNotEqual(before.digest, after.digest)
        self.policy["executableVersion"] = "new-version"
        self.rehash()
        _, after = isolation._binding(self.request)
        self.assertNotEqual(before.digest, after.digest)

    def test_pre_prompt_evidence_cannot_cover_policy_change_at_launch(self):
        isolation.pre_prompt(self.request)
        self.policy["allowedPaths"].remove(self.request["databasePath"])
        self.rehash()
        with self.assertRaises(isolation.IsolationUnavailable):
            self.launch()
        self.assertEqual(self.backend.spawned, [])

    def test_executable_change_after_pre_prompt_refuses_launch(self):
        isolation.pre_prompt(self.request)
        Path(self.request["executable"]).write_bytes(b"changed executable")
        with self.assertRaisesRegex(
            isolation.IsolationUnavailable, "executable digest"
        ):
            self.launch()
        self.assertEqual(self.backend.spawned, [])

    def test_alias_paths_and_incomplete_boundary_refuse(self):
        self.request["cwd"] += "/../" + self.root.name
        with self.assertRaisesRegex(isolation.IsolationUnavailable, "canonical"):
            self.launch()
        self.request["cwd"] = str(self.root)
        self.policy["deniedResources"] = []
        self.rehash()
        with self.assertRaisesRegex(
            isolation.IsolationUnavailable, "required credential boundary"
        ):
            self.launch()

    def test_command_change_cannot_escape_bound_executable(self):
        with self.assertRaisesRegex(isolation.IsolationUnavailable, "command differs"):
            isolation.launch(self.request, ["/bin/sh"], cwd=self.request["cwd"])
        self.assertEqual(self.backend.spawned, [])

    def test_bad_process_evidence_is_aborted_before_prompt_delivery(self):
        self.backend.change_evidence = lambda evidence: (
            replace(evidence, process_id=999)
            if evidence.stage == "process"
            else evidence
        )
        with self.assertRaises(isolation.IsolationUnavailable):
            self.launch()
        self.assertEqual(self.backend.aborted, [12345])

    def test_launch_exception_is_uncertain_and_has_no_fallback(self):
        with patch.object(
            self.backend, "launch", side_effect=OSError("synthetic unknown")
        ):
            with self.assertRaises(isolation.IsolationLaunchUncertain):
                self.launch()

    def test_unproved_cleanup_is_uncertain(self):
        self.backend.change_evidence = lambda evidence: (
            replace(evidence, process_id=999)
            if evidence.stage == "process"
            else evidence
        )
        with patch.object(self.backend, "abort", return_value=None):
            with self.assertRaises(isolation.IsolationLaunchUncertain):
                self.launch()

    def test_status_is_read_only_and_needs_no_policy_or_backend_probe(self):
        with patch.object(
            paths, "config", side_effect=AssertionError("no configuration read")
        ):
            self.assertFalse(isolation.status()["ready"])


class IntegrationTests(unittest.TestCase):
    def test_send_refuses_before_prompt_is_persisted_or_wrapper_launched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sid = "ses_TEST"
            slot = {
                "sessionId": sid,
                "databasePath": str(root / "fake.db"),
                "cwd": str(root),
                "model": opencode.MODEL,
                "executable": "/synthetic-executable",
                "dispatchEnabled": True,
            }
            native = {
                "sessionCwd": str(root),
                "databasePath": str(root / "fake.db"),
                "nativeModelProvider": "opencode",
                "nativeModelId": "deepseek-v4.1-flash-free",
            }
            with (
                patch.object(paths, "OPENCODE", root / "data"),
                patch.object(paths, "LOGS", root / "logs"),
                patch.object(paths, "config", return_value={}),
                patch.object(opencode, "verify_slot", return_value=slot),
                patch.object(opencode, "state", return_value={"state": "idle"}),
                patch.object(opencode, "verify_native_session", return_value=native),
                patch.object(opencode.subprocess, "Popen") as popen,
            ):
                with self.assertRaises(isolation.IsolationUnavailable):
                    opencode.send(slot, "synthetic prompt", "task")
                popen.assert_not_called()
                self.assertEqual(list(root.rglob("request.json")), [])

    def test_run_turn_rechecks_missing_policy_and_never_starts_unsandboxed_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            turn = root / "ses_TEST" / "turns" / "turn-1"
            turn.mkdir(parents=True)
            request = {
                "sessionId": "ses_TEST",
                "cwd": str(root),
                "sessionCwd": str(root),
                "model": opencode.MODEL,
                "turn": "turn-1",
                "taskId": "task",
                "databasePath": str(root / "fake.db"),
                "lockFd": 5,
                "logDir": str(root / "logs"),
                "executable": "/synthetic-executable",
                "message": "synthetic prompt",
                "isolationLaunchId": "instance",
            }
            request_path = turn / "request.json"
            request_path.write_text(json.dumps(request))
            native = {
                "databasePath": request["databasePath"],
                "nativeModelProvider": "opencode",
                "nativeModelId": "deepseek-v4.1-flash-free",
            }
            with (
                patch.object(paths, "config", return_value={}),
                patch.object(opencode, "_verify_inherited_lock"),
                patch.object(opencode, "verify_native_session", return_value=native),
                patch.object(opencode, "_runtime_environment", return_value={}),
                patch.object(opencode.subprocess, "Popen") as popen,
            ):
                result = opencode.run_turn(request_path)
                self.assertEqual(result["state"], "blocked")
                self.assertIn("policy is absent", result["error"])
                popen.assert_not_called()
