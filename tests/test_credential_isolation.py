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
                2,
                binding.backend_id,
                self.version,
                binding.policy_digest,
                binding.digest,
                stage,
                now - 0.01,
                now + 10,
                process_id,
                "fake-domain" if stage in {"process", "cleanup"} else None,
                b"fake-proof",
            )
        )

    def verify(self, evidence, binding, stage, process_id):
        return evidence.proof == b"fake-proof"

    def launch(self, binding, args, **kwargs):
        self.spawned.append((binding, args, kwargs))
        process = SimpleNamespace(pid=12345)
        return process, "fake-domain", self.attest(binding, "process", process.pid)

    def abort(self, process, binding, containment_domain):
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
            "contractVersion": 2,
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
        self.assertEqual(self.launch().process.pid, 12345)
        self.assertEqual(len(self.backend.spawned), 1)

    def test_absent_unknown_or_mismatched_contract_refuses(self):
        cases = (
            {},
            {**self.policy, "backendId": "unknown"},
            {**self.policy, "contractVersion": 99},
            {**self.policy, "contractVersion": 1},
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

    def test_cleanup_for_another_domain_cannot_certify_the_launched_domain(self):
        handle = self.launch()
        self.backend.change_evidence = lambda evidence: replace(
            evidence, containment_domain="foreign-domain"
        )
        with self.assertRaises(isolation.IsolationLaunchUncertain):
            handle.finish()

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


class RecoveryBindingTests(unittest.TestCase):
    setUp = ContractTests.setUp
    rehash = ContractTests.rehash
    launch = ContractTests.launch

    def recovery(self):
        owned = self.root / "owned-checkout"
        owned.mkdir(exist_ok=True)
        self.request.update(
            recoveryId="recovery-1", ownedWorktree=str(owned), branch="repair"
        )
        self.policy["allowedPaths"].append(str(owned))
        self.rehash()
        return owned

    def test_distinct_native_and_owned_directories_are_both_bound(self):
        owned = self.recovery()
        _, binding = isolation._binding(self.request)
        self.assertEqual(binding.cwd, str(self.root))
        self.assertEqual(binding.owned_worktree, str(owned))
        self.assertEqual(binding.recovery_id, "recovery-1")
        self.assertEqual(binding.recovery_branch, "repair")
        isolation.pre_prompt(self.request)
        self.launch()

    def test_recovery_checkout_requires_its_own_permission(self):
        owned = self.recovery()
        self.policy["allowedPaths"].remove(str(owned))
        self.rehash()
        with self.assertRaisesRegex(isolation.IsolationUnavailable, "recovery"):
            isolation.pre_prompt(self.request)
        with self.assertRaisesRegex(isolation.IsolationUnavailable, "recovery"):
            self.launch()
        self.assertEqual(self.backend.spawned, [])

    def test_recovery_alias_is_refused(self):
        owned = self.recovery()
        alias = self.root / "owned-alias"
        alias.symlink_to(owned, target_is_directory=True)
        self.request["ownedWorktree"] = str(alias)
        with self.assertRaisesRegex(isolation.IsolationUnavailable, "canonical"):
            self.launch()

    def test_changed_recovery_target_cannot_reuse_pre_prompt_binding(self):
        self.recovery()
        other = self.root / "other-owned"
        other.mkdir()
        self.policy["allowedPaths"].append(str(other))
        self.rehash()
        _, before = isolation._binding(self.request)
        isolation.pre_prompt(self.request)
        self.request["ownedWorktree"] = str(other)
        self.backend.change_evidence = lambda evidence: replace(
            evidence, binding_digest=before.digest
        )
        with self.assertRaisesRegex(isolation.IsolationUnavailable, "mismatched"):
            self.launch()
        self.assertEqual(self.backend.spawned, [])


class TerminalDomainTests(unittest.TestCase):
    def setUp(self):
        from .test_opencode import OpenCodeAdapterTests

        self.adapter = OpenCodeAdapterTests(
            "test_dispatch_lock_prevents_concurrent_session_turns"
        )
        self.adapter.setUp()
        self.addCleanup(self.adapter.doCleanups)
        self.authority = ContractTests(
            "test_status_is_read_only_and_needs_no_policy_or_backend_probe"
        )
        self.authority.setUp()
        self.addCleanup(self.authority.doCleanups)
        self.backend = self.authority.backend
        _, self.binding = isolation._binding(self.authority.request)
        self.handles = []

        def launch(request, args, **kwargs):
            process = opencode.subprocess.Popen(args, **kwargs)
            handle = isolation.LaunchHandle(
                self.backend, self.binding, process, "fake-domain"
            )
            self.handles.append(handle)
            return handle

        gate = patch.object(isolation, "launch", side_effect=launch)
        gate.start()
        self.addCleanup(gate.stop)

    def events(self):
        return [
            self.adapter.event(
                "step_start", {"type": "step-start", "messageID": "msg_1"}
            ),
            self.adapter.event(
                "text", {"type": "text", "messageID": "msg_1", "text": "OK"}
            ),
            self.adapter.event(
                "step_finish",
                {"type": "step-finish", "messageID": "msg_1", "reason": "stop"},
            ),
        ]

    def assert_uncertain(self, result, request):
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["cleanup"], "uncertain")
        self.assertEqual(len(self.backend.aborted), 1)
        persisted = json.loads((request.parent / "state.json").read_text())
        self.assertEqual(persisted["cleanup"], "uncertain")
        result_path = request.parent / "result.json"
        if result_path.exists():
            self.assertEqual(
                json.loads(result_path.read_text())["cleanup"], "uncertain"
            )

    def test_normal_success_needs_authenticated_domain_cleanup(self):
        request = self.adapter.request()
        with patch.object(self.backend, "abort", wraps=self.backend.abort) as abort:
            result, _, _ = self.adapter.run_events(
                request, self.events(), completion=True
            )
        self.assertEqual(result["state"], "done")
        abort.assert_called_once()

    def test_host_exit_cannot_certify_backend_domain_on_success(self):
        request = self.adapter.request()
        self.backend.change_evidence = lambda evidence: replace(
            evidence, proof=b"invalid"
        )
        result, _, _ = self.adapter.run_events(request, self.events(), completion=True)
        self.assert_uncertain(result, request)

    def test_host_cleanup_cannot_certify_backend_domain_on_cancellation(self):
        request = self.adapter.request()
        self.backend.change_evidence = lambda evidence: replace(
            evidence, proof=b"invalid"
        )
        with patch.object(opencode, "_cancel_requested", return_value=True):
            result, _, _ = self.adapter.run_events(request, [])
        self.assert_uncertain(result, request)

    def test_host_cleanup_cannot_certify_backend_domain_on_timeout(self):
        import itertools

        request = self.adapter.request(timeout_seconds=1)
        self.backend.change_evidence = lambda evidence: replace(
            evidence, proof=b"invalid"
        )
        with patch.object(
            opencode.time, "monotonic", side_effect=itertools.count(0, 5)
        ):
            result, _, _ = self.adapter.run_events(request, [])
        self.assert_uncertain(result, request)

    def test_host_cleanup_cannot_certify_backend_domain_on_fatal_error(self):
        request = self.adapter.request()
        self.backend.change_evidence = lambda evidence: replace(
            evidence, proof=b"invalid"
        )
        events = [
            self.adapter.event("error", error={"message": "synthetic fatal error"})
        ]
        result, _, _ = self.adapter.run_events(request, events)
        self.assert_uncertain(result, request)

    def test_post_launch_identity_failure_still_requires_domain_cleanup(self):
        from .test_opencode import FakeProcess

        request = self.adapter.request()
        self.backend.change_evidence = lambda evidence: replace(
            evidence, proof=b"invalid"
        )
        process = FakeProcess()
        with (
            patch.object(opencode.subprocess, "Popen", return_value=process),
            patch.object(opencode, "process_identity", return_value=None),
        ):
            result = opencode.run_turn(request)
        process.stdout.close()
        self.assert_uncertain(result, request)
