"""Offline proof only: synthetic quota signatures are not live provider evidence."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from fleet import disposable as d


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        executable = self.root / "opencode"
        executable.write_text("stub")
        executable.chmod(0o755)
        self.path = self.root / "quota.json"
        self.config = d.Config.parse({"executable": str(executable), "account": "opencode",
                                      "cwd": str(self.root), "model": d.MODEL,
                                      "maxConcurrent": 3, "fallbackModel": d.PAID_MODEL,
                                      "quotaEvidencePath": str(self.path)})
        self.executor = d.NativeExecutor()
        self.executor._POLL_SECONDS = 0
        self.calls = []
        self.model = {"providerID": "opencode", "id": "mimo-v2.6-flash-free"}
        self.frames = []
        self.cancel = threading.Event()
        self.providers = [{"id": "opencode", "integrationID": "opencode"},
                          {"id": "opencode-go", "integrationID": "opencode"}]
        self.sid = "ses_fallback_test"
        self.job = d.Job("approved", "Original approved task with partial side effects")
        self.patchers = [
            patch.object(self.executor, "_call", side_effect=self.api),
            patch.object(self.executor, "_session_tree", side_effect=lambda c, s: [s]),
            patch.object(self.executor, "_active_ids", return_value=set()),
            patch.object(self.executor, "job_cwd", return_value=str(self.root)),
            patch.object(self.executor, "_messages", side_effect=self.messages),
            patch("fleet.disposable.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, d.MODEL + "\n" + d.PAID_MODEL + "\n")),
        ]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)

    def api(self, config, method, path, body=None, **kwargs):
        self.calls.append((method, path, body, dict(self.model)))
        if path == "/api/provider":
            return {"data": self.providers}
        if method == "POST" and path == "/api/session":
            self.model = body["model"]
            return {"data": {"id": self.sid}}
        if method == "POST" and path.endswith("/model"):
            self.model = body["model"]
            return None
        if method == "GET" and path == "/api/session/" + self.sid:
            return {"data": {"id": self.sid, "directory": str(self.root), "model": self.model}}
        if method == "GET" and path.endswith("/permission"):
            return {"data": []}
        return {"data": {"id": "msg_user"}}

    def messages(self, *args):
        if len(self.frames) > 1:
            return [self.frames.pop(0)]
        return self.frames

    @staticmethod
    def success():
        return {"id": "msg_paid_done", "type": "assistant", "finish": "stop",
                "content": [{"type": "text", "text": "completed from existing context"}]}

    @staticmethod
    def quota(model=d.MODEL):
        return {"id": "msg_free_quota" if model == d.MODEL else "msg_paid_quota",
                "type": "assistant", "finish": "error", "error": {
                    "type": "FixtureQuota", "status": 429,
                    "message": "Fixture allocation exhausted for this model"}}

    @staticmethod
    def signature(model=d.MODEL):
        return {"model": model, "verified": True,
                "evidence": "Offline fixture explicitly verified exhaustion signature",
                "type": "FixtureQuota", "status": 429,
                "messagePrefix": "Fixture allocation exhausted"}

    def state(self, held=False, signatures=()):
        state = {"version": 1, "holds": {}, "signatures": list(signatures)}
        if held:
            state["holds"][d.MODEL] = {
                "verified": True, "evidence": "Root verified existing free usage exhaustion"}
        self.path.write_text(json.dumps(state))

    def run_job(self, config=None):
        config = config or self.config
        self.executor.verify(config)
        sid = self.executor.create(config, self.job)
        return self.executor.run(config, sid, self.job, self.cancel)

    def prompts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/prompt")]

    def switches(self):
        return [c for c in self.calls if c[1].endswith("/model")]

    def test_held_free_admission_never_calls_free_provider_and_uses_same_session(self):
        self.state(held=True)
        self.frames = [self.success()]
        result = self.run_job()
        self.assertEqual(result.state, "finished")
        self.assertEqual(len(self.prompts()), 1)
        self.assertEqual(self.prompts()[0][2], {"text": self.job.message})
        self.assertEqual(self.prompts()[0][3]["providerID"], "opencode-go")
        self.assertEqual(len(self.switches()), 1)
        self.assertEqual(self.switches()[0][1], "/api/session/" + self.sid + "/model")
        self.assertEqual(sum(c[1] == "/api/session" for c in self.calls), 1)

    def test_verified_quota_continues_once_without_replaying_original_prompt(self):
        self.state(signatures=[self.signature()])
        self.frames = [self.quota(), self.quota(), self.success()]
        result = self.run_job()
        self.assertEqual(result.state, "finished")
        prompts = self.prompts()
        self.assertEqual(len(prompts), 2)
        self.assertEqual(sum(c[2]["text"] == self.job.message for c in prompts), 1)
        self.assertIn("partial work first", prompts[1][2]["text"])
        self.assertTrue(prompts[1][2]["id"].startswith("msg_fallback_"))
        self.assertEqual(prompts[0][1], prompts[1][1])
        self.assertFalse(any("fork" in c[1] for c in self.calls))
        self.assertIn(d.MODEL, json.loads(self.path.read_text())["holds"])
        self.assertEqual(len(result.transitions), 1)

    def test_generic_rate_auth_and_network_errors_do_not_trigger_paid(self):
        for status, message in [(429, "Too many requests; retry later"),
                                (401, "Fixture allocation exhausted for this model"),
                                (503, "Network unavailable"),
                                (429, "quota exceeded without verified signature")]:
            with self.subTest(status=status, message=message):
                self.calls.clear()
                self.state(signatures=[self.signature()])
                frame = self.quota()
                frame["error"].update(status=status, message=message)
                self.frames = [frame]
                result = self.run_job()
                self.assertEqual(result.state, "failed")
                self.assertTrue(result.preserve_session)
                self.assertEqual(len(self.prompts()), 1)
                self.assertEqual(self.switches(), [])
                self.assertEqual(json.loads(self.path.read_text())["holds"], {})

    def test_another_jobs_quota_hold_does_not_promote_this_jobs_generic_error(self):
        self.state(signatures=[self.signature()])
        frame = self.quota()
        frame["error"] = {"type": "Network", "status": 503, "message": "unavailable"}
        def interleaved_messages(*args):
            # Both turns began free. The other job records genuine verified
            # quota while this job is awaiting an unrelated provider result.
            d.NativeExecutor()._record_quota(
                self.config, d.MODEL, {"id": "msg_other_job"}, self.signature())
            return [frame]
        with patch.object(self.executor, "_messages", side_effect=interleaved_messages):
            result = self.run_job()
        self.assertEqual(result.state, "failed")
        self.assertTrue(result.preserve_session)
        self.assertFalse(self.switches())
        self.assertEqual(len(self.prompts()), 1)
        self.assertEqual(json.loads(self.path.read_text())["holds"][d.MODEL]["assistantID"],
                         "msg_other_job")

    def test_quota_sounding_error_without_verified_signature_does_not_switch(self):
        self.state()
        self.frames = [self.quota()]
        result = self.run_job()
        self.assertTrue(result.preserve_session)
        self.assertFalse(self.switches())

    def test_unverified_or_malformed_evidence_refuses_before_prompt(self):
        for state in [{"version": True, "holds": {}, "signatures": []},
                      {"version": 1, "holds": {d.MODEL: {"verified": False,
                       "evidence": "unverified usage observation"}}, "signatures": []}]:
            self.path.write_text(json.dumps(state))
            with self.assertRaises(d.AdmissionRefused):
                self.executor.verify(self.config)
        self.path.write_text('{"partial":')
        with self.assertRaises(d.AdmissionRefused):
            self.executor.verify(self.config)
        self.assertFalse(self.calls)

    def test_paid_exhaustion_is_separate_persistent_stop_not_another_retry(self):
        self.state(signatures=[self.signature(), self.signature(d.PAID_MODEL)])
        self.frames = [self.quota(), self.quota(d.PAID_MODEL)]
        result = self.run_job()
        self.assertTrue(result.preserve_session)
        self.assertEqual(result.state, "failed")
        self.assertEqual(len(self.prompts()), 2)
        self.assertEqual(len(self.switches()), 1)
        state = json.loads(self.path.read_text())
        self.assertEqual(set(state["holds"]), {d.MODEL, d.PAID_MODEL})
        self.calls.clear()
        with self.assertRaisesRegex(d.AdmissionRefused, "Paid model is held"):
            d.NativeExecutor().verify(self.config)
        self.assertFalse(self.calls)

    def test_unclassified_paid_failure_is_not_mislabeled_quota(self):
        self.state(held=True)
        failure = self.quota(d.PAID_MODEL)
        failure["error"] = {"type": "Network", "status": 503, "message": "unavailable"}
        self.frames = [failure]
        result = self.run_job()
        self.assertTrue(result.preserve_session)
        state = json.loads(self.path.read_text())
        self.assertIn("paidFailure", state)
        self.assertNotIn(d.PAID_MODEL, state["holds"])
        with self.assertRaises(d.AdmissionRefused):
            self.executor.verify(self.config)

    def test_missing_assistant_identity_never_continues_a_turn(self):
        self.state(signatures=[self.signature()])
        frame = self.quota()
        del frame["id"]
        self.frames = [frame]
        result = self.run_job()
        self.assertTrue(result.preserve_session)
        self.assertFalse(self.switches())
        self.assertEqual(len(self.prompts()), 1)

    def test_ambiguous_original_prompt_is_not_replayed(self):
        original_api = self.api
        def api(config, method, path, body=None, **kwargs):
            result = original_api(config, method, path, body, **kwargs)
            if path.endswith("/prompt"):
                raise d.OpenCodeApiError("Ambiguous prompt response")
            return result
        with patch.object(self.executor, "_call", side_effect=api):
            with self.assertRaises(d.OpenCodeApiError):
                self.run_job()
        self.assertEqual(len(self.prompts()), 1)
        self.assertFalse(self.switches())

    def test_ambiguous_switch_never_sends_continuation_or_recreates_session(self):
        self.state(signatures=[self.signature()])
        self.frames = [self.quota()]
        original_api = self.api
        def api(config, method, path, body=None, **kwargs):
            result = original_api(config, method, path, body, **kwargs)
            if path.endswith("/model"):
                raise d.OpenCodeApiError("Ambiguous switch response")
            return result
        with patch.object(self.executor, "_call", side_effect=api):
            with self.assertRaises(d.OpenCodeApiError):
                self.run_job()
        self.assertEqual(len(self.prompts()), 1)
        self.assertEqual(len(self.switches()), 1)
        self.assertEqual(sum(c[1] == "/api/session" for c in self.calls), 1)

    def test_switch_readback_must_preserve_session_model_and_directory(self):
        self.state(held=True)
        original_api = self.api
        def api(config, method, path, body=None, **kwargs):
            result = original_api(config, method, path, body, **kwargs)
            if method == "GET" and path == "/api/session/" + self.sid and self.switches():
                result["data"]["directory"] = "/other"
            return result
        with patch.object(self.executor, "_call", side_effect=api):
            with self.assertRaisesRegex(d.AdmissionRefused, "preserve session"):
                self.run_job()
        self.assertFalse(self.prompts())

    def test_cancel_after_switch_prevents_paid_prompt(self):
        self.state(held=True)
        original_api = self.api
        def api(config, method, path, body=None, **kwargs):
            result = original_api(config, method, path, body, **kwargs)
            if path.endswith("/model"):
                self.cancel.set()
            return result
        with patch.object(self.executor, "_call", side_effect=api):
            result = self.run_job()
        self.assertEqual(result.state, "cancelled")
        self.assertFalse(self.prompts())

    def test_direct_paid_requires_free_hold_and_real_integration_binding(self):
        paid = replace(self.config, model=d.PAID_MODEL, account="opencode-go", fallback_model=None)
        with self.assertRaisesRegex(d.AdmissionRefused, "verified free-quota hold"):
            self.executor.verify(paid)
        self.state(held=True)
        with self.assertRaisesRegex(d.AdmissionRefused, "provider does not match"):
            self.executor.verify(replace(paid, account="opencode"))
        self.executor.verify(paid)
        self.providers[1]["integrationID"] = "opencode-go"
        with self.assertRaises(d.AdmissionRefused):
            self.executor.verify(paid)

    def test_paid_native_failure_keeps_session_and_closes_fleet_admission(self):
        self.state(held=True)
        self.frames = [self.quota(d.PAID_MODEL)]
        fleet = d.Fleet(self.config, self.executor)
        fleet.enqueue({"id": self.job.id, "message": self.job.message, "rootAuthorized": True})
        fleet.drain()
        status = fleet.status()
        self.assertTrue(status["blocked"])
        self.assertEqual(status["results"][0]["sessionId"], self.sid)
        self.assertFalse(status["results"][0]["retired"])
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))
        self.assertEqual(len(self.prompts()), 1)
        with self.assertRaises(d.AdmissionRefused):
            fleet.enqueue({"id": "another", "message": "do", "rootAuthorized": True})

    def test_paid_timeout_interrupt_inactive_preserves_session_and_blocks_admission(self):
        for held_at_start in (True, False):
            with self.subTest(held_at_start=held_at_start):
                self.calls.clear()
                self.state(held=held_at_start, signatures=[self.signature()])
                self.cancel.clear()
                activity = {"running": False}
                frames = [] if held_at_start else [self.quota()]
                original_api = self.api
                def api(config, method, path, body=None, **kwargs):
                    result = original_api(config, method, path, body, **kwargs)
                    if path.endswith("/prompt") and self.model["providerID"] == "opencode-go":
                        activity["running"] = True
                    if path.endswith("/interrupt"):
                        activity["running"] = False
                    return result
                def messages(*args):
                    return [frames.pop(0)] if frames else []
                with patch.object(self.executor, "_call", side_effect=api), \
                        patch.object(self.executor, "_messages", side_effect=messages), \
                        patch.object(self.executor, "_active_ids", side_effect=lambda c:
                                     {self.sid} if activity["running"] else set()), \
                        patch.object(self.executor, "_JOB_TIMEOUT", 0), \
                        patch.object(self.executor, "_STOP_TIMEOUT", 5), \
                        patch("fleet.disposable.time.monotonic", return_value=100), \
                        patch.object(self.executor, "retire", return_value=True) as retire:
                    fleet = d.Fleet(self.config, self.executor)
                    fleet.enqueue({"id": self.job.id, "message": self.job.message,
                                   "rootAuthorized": True})
                    fleet.drain()
                    status = fleet.status()
                    retire.assert_not_called()
                    self.assertTrue(status["blocked"])
                    terminal = status["results"][0]
                    self.assertEqual(terminal["state"], "failed")
                    self.assertFalse(terminal["retired"])
                    self.assertEqual(terminal["sessionId"], self.sid)
                    self.assertIn("timed out", terminal["error"])
                    self.assertFalse(any(c[0] == "DELETE" for c in self.calls))
                    self.assertEqual(sum(c[1].endswith("/interrupt") for c in self.calls), 1)
                    self.assertEqual(len(self.prompts()), 1 if held_at_start else 2)
                    with self.assertRaises(d.AdmissionRefused):
                        fleet.enqueue({"id": "next", "message": "do", "rootAuthorized": True})
                    with self.assertRaisesRegex(d.AdmissionRefused, "Paid model is held"):
                        self.executor.verify(self.config)
                state = json.loads(self.path.read_text())
                self.assertEqual(state["paidFailure"]["kind"], "timeout")
                self.assertNotIn(d.PAID_MODEL, state["holds"])

    def test_operator_cancel_of_paid_turn_does_not_latch_paid_failure(self):
        self.state(held=True)
        self.frames = []
        activity = {"running": False}
        original_api = self.api
        def api(config, method, path, body=None, **kwargs):
            result = original_api(config, method, path, body, **kwargs)
            if path.endswith("/prompt"):
                activity["running"] = True
                self.cancel.set()
            if path.endswith("/interrupt"):
                activity["running"] = False
            return result
        with patch.object(self.executor, "_call", side_effect=api), \
                patch.object(self.executor, "_active_ids", side_effect=lambda c:
                             {self.sid} if activity["running"] else set()), \
                patch.object(self.executor, "_JOB_TIMEOUT", 0):
            result = self.run_job()
        self.assertEqual(result.state, "cancelled")
        self.assertTrue(result.stopped)
        self.assertFalse(result.preserve_session)
        self.assertNotIn("paidFailure", json.loads(self.path.read_text()))
        self.assertEqual(len(self.prompts()), 1)

    def test_unconfirmed_paid_timeout_still_latches_failure_without_claiming_stop(self):
        self.state(held=True)
        self.frames = []
        activity = {"running": False}
        original_api = self.api
        def api(config, method, path, body=None, **kwargs):
            result = original_api(config, method, path, body, **kwargs)
            if path.endswith("/prompt"):
                activity["running"] = True
            return result  # Interrupt remains unconfirmed.
        with patch.object(self.executor, "_call", side_effect=api), \
                patch.object(self.executor, "_active_ids", side_effect=lambda c:
                             {self.sid} if activity["running"] else set()), \
                patch.object(self.executor, "_JOB_TIMEOUT", 0), \
                patch.object(self.executor, "_STOP_TIMEOUT", 0), \
                patch("fleet.disposable.time.monotonic", return_value=100):
            result = self.run_job()
        self.assertEqual(result.state, "failed")
        self.assertFalse(result.stopped)
        self.assertTrue(result.preserve_session)
        self.assertEqual(json.loads(self.path.read_text())["paidFailure"]["kind"], "timeout")
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))

    def test_paid_timeout_hold_is_durable_even_when_interrupt_fails(self):
        self.state(held=True)
        self.frames = []
        activity = {"running": False}
        original_api = self.api
        def api(config, method, path, body=None, **kwargs):
            result = original_api(config, method, path, body, **kwargs)
            if path.endswith("/prompt"):
                activity["running"] = True
            if path.endswith("/interrupt"):
                raise d.OpenCodeApiError("Interrupt transport failed")
            return result
        with patch.object(self.executor, "_call", side_effect=api), \
                patch.object(self.executor, "_active_ids", side_effect=lambda c:
                             {self.sid} if activity["running"] else set()), \
                patch.object(self.executor, "_JOB_TIMEOUT", 0), \
                patch("fleet.disposable.time.monotonic", return_value=100), \
                patch.object(self.executor, "retire", return_value=True) as retire:
            fleet = d.Fleet(self.config, self.executor)
            fleet.enqueue({"id": self.job.id, "message": self.job.message, "rootAuthorized": True})
            fleet.drain()
            self.assertTrue(fleet.status()["blocked"])
            retire.assert_not_called()
            self.assertEqual(json.loads(self.path.read_text())["paidFailure"]["kind"], "timeout")
            with self.assertRaisesRegex(d.AdmissionRefused, "Paid model is held"):
                self.executor.verify(self.config)
        self.assertFalse(any(c[0] == "DELETE" for c in self.calls))

    def test_cancel_between_loop_check_and_deadline_is_not_paid_timeout(self):
        self.state(held=True)
        self.frames = []
        activity = {"running": False}
        clock_calls = []
        original_api = self.api
        def api(config, method, path, body=None, **kwargs):
            result = original_api(config, method, path, body, **kwargs)
            if path.endswith("/prompt"):
                activity["running"] = True
            if path.endswith("/interrupt"):
                activity["running"] = False
            return result
        def clock():
            clock_calls.append(1)
            if len(clock_calls) == 2:
                self.cancel.set()  # After loop-top check, before timeout decision.
            return 100
        with patch.object(self.executor, "_call", side_effect=api), \
                patch.object(self.executor, "_active_ids", side_effect=lambda c:
                             {self.sid} if activity["running"] else set()), \
                patch.object(self.executor, "_JOB_TIMEOUT", 0), \
                patch.object(self.executor, "_STOP_TIMEOUT", 5), \
                patch("fleet.disposable.time.monotonic", side_effect=clock):
            result = self.run_job()
        self.assertEqual(result.state, "cancelled")
        self.assertTrue(result.stopped)
        self.assertFalse(result.preserve_session)
        self.assertNotIn("paidFailure", json.loads(self.path.read_text()))
        self.assertEqual(len(self.prompts()), 1)

    def test_free_and_go_managers_share_the_same_three_slot_lock(self):
        paid = replace(self.config, model=d.PAID_MODEL, account="opencode-go", fallback_model=None)
        with patch("fleet.disposable.Path.home", return_value=self.root):
            fd = d._acquire_manager_lock(self.config)
            try:
                with self.assertRaisesRegex(d.AdmissionRefused, "already owns"):
                    d._acquire_manager_lock(paid)
            finally:
                os.close(fd)

    def test_guessed_models_or_expanded_cap_cannot_enter_fallback(self):
        for changes in [{"fallbackModel": "opencode/other"}, {"maxConcurrent": 4},
                        {"model": d.PAID_MODEL, "fallbackModel": None,
                         "quotaEvidencePath": None}]:
            with self.subTest(changes=changes), self.assertRaises(d.AdmissionRefused):
                d.Config.parse({**self.config.raw(), **changes})


if __name__ == "__main__":
    unittest.main()
