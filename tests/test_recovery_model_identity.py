"""Cancellation and recovery validate the model configured for the target session.

The cancellation and recovery paths used to compare the persisted launch and
turn receipts against one fleet-wide constant. A session configured with a
different free model could therefore never be cancelled or recovered, and the
turn it had actually run was never checked against its own configuration.

These tests pin the repaired contract:

* a fully matching configured-model turn is accepted;
* every other identity link still fails closed, one link at a time;
* rate-limit and quota failures are still refused;
* the repaired paths make no provider or model call.

Nothing here starts a model, a provider or the OpenCode CLI.
"""
import inspect
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import opencode, paths, recovery  # noqa: E402

# A free model that is not the fleet-wide default constant.
CONFIGURED_MODEL = "opencode-go/minimax-m3.1-flash-free"
OTHER_MODEL = "opencode/deepseek-v4.1-flash-free"


class ConfiguredModelIdentityTests(unittest.TestCase):
    """The receipts are validated against the session's configured model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.native_cwd = self.root / "native-worker"
        self.native_cwd.mkdir()
        self.other_cwd = self.root / "other-worker"
        self.other_cwd.mkdir()
        self.sid = "ses_configured_model_test"
        self.other_sid = "ses_configured_model_other"
        self.task_id = "failed-task"
        self.turn = "turn_failed_configured"
        self.database_path = self.root / "native.db"
        connection = sqlite3.connect(self.database_path)
        connection.close()
        self.cwd = str(self.native_cwd.resolve())
        self.slot = {"sessionId": self.sid, "cwd": self.cwd,
                     "sessionCwd": self.cwd,
                     "databasePath": str(self.database_path),
                     "model": CONFIGURED_MODEL, "provider": "opencode",
                     "dispatchEnabled": True}
        self.data = self.root / "data"
        self.queue = self.data / "queue.json"
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("OPENCODE", self.data / "opencode"),
                            ("LOGS", self.root / "logs"),
                            ("BIN", self.root / "bin" / "fleet"),
                            ("QUEUE", self.queue),
                            ("QUEUE_LOCK", self.data / "queue.lock"),
                            ("PAUSE", self.data / "dispatch.paused")):
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        slot_patch = patch.object(paths, "opencode_slots", return_value=[self.slot])
        slot_patch.start()
        self.addCleanup(slot_patch.stop)
        exec_patch = patch.object(paths, "executable", return_value="/usr/bin/opencode")
        exec_patch.start()
        self.addCleanup(exec_patch.stop)

    # -- fixtures ---------------------------------------------------------
    def write_receipts(self, model=CONFIGURED_MODEL, state="error", cleanup="confirmed",
                       error="OpenCode turn did not produce a verified native completion",
                       tool_failures=None, process_identity=None, launch=None, state_extra=None,
                       result_extra=None):
        session = self.data / "opencode" / self.sid
        turn_dir = session / "turns" / self.turn
        turn_dir.mkdir(parents=True, exist_ok=True)
        launch = launch if launch is not None else {
            "turn": self.turn, "taskId": self.task_id, "sessionId": self.sid,
            "cwd": self.cwd, "model": model}
        (session / "current.json").write_text(json.dumps(launch))
        state_receipt = dict(launch, state=state, cleanup=cleanup, error=error,
                             finishedAt=100)
        if state_extra:
            state_receipt.update(state_extra)
        (turn_dir / "state.json").write_text(json.dumps(state_receipt))
        result = dict(turn=self.turn, taskId=self.task_id, sessionId=self.sid, cwd=self.cwd,
                      expectedModel=model,
                      state=state, cleanup=cleanup, error=error, toolFailures=[],
                      finishedAt=100,
                      managedProcessIdentity={"pid": 9999999, "pgid": 9999999,
                                              "start": [1], "executable": "/usr/bin/opencode"})
        if tool_failures is not None:
            result["toolFailures"] = tool_failures
        if process_identity is not None:
            result["managedProcessIdentity"] = process_identity
        if result_extra:
            result.update(result_extra)
        (turn_dir / "result.json").write_text(json.dumps(result))
        return turn_dir

    def write_working_turn(self, model=CONFIGURED_MODEL, identity=None, state_extra=None):
        session = self.data / "opencode" / self.sid
        turn_dir = session / "turns" / self.turn
        turn_dir.mkdir(parents=True, exist_ok=True)
        (session / "current.json").write_text(json.dumps({
            "turn": self.turn, "taskId": self.task_id, "sessionId": self.sid,
            "cwd": self.cwd, "model": model}))
        identity = identity if identity is not None else {
            "pid": 4242, "pgid": 4242, "start": [1], "executable": "/usr/bin/opencode"}
        receipt = {"sessionId": self.sid, "cwd": self.cwd, "model": model,
                   "state": "working", "processIdentity": identity}
        if state_extra:
            receipt.update(state_extra)
        (turn_dir / "state.json").write_text(json.dumps(receipt))
        return turn_dir

    # -- acceptance -------------------------------------------------------
    def test_configured_model_failed_turn_is_accepted(self):
        self.write_receipts(model=CONFIGURED_MODEL)
        launch, state, result = recovery.verify_failed_turn(
            self.sid, self.task_id, self.turn, self.cwd, CONFIGURED_MODEL)
        self.assertEqual(launch["model"], CONFIGURED_MODEL)
        self.assertEqual(state["model"], CONFIGURED_MODEL)
        self.assertEqual(state["cleanup"], "confirmed")
        self.assertEqual(result["state"], "error")
        recovery._verify_pointer_identity(self.sid, self.task_id, self.turn, self.cwd,
                                          CONFIGURED_MODEL)

    def test_configured_model_turn_is_cancellable(self):
        turn_dir = self.write_working_turn(model=CONFIGURED_MODEL)
        with patch.object(opencode, "identity_matches", return_value=True):
            result = opencode.cancel(self.slot)
        self.assertEqual(result, {"cancelRequested": 4242, "turn": self.turn})
        command = json.loads((turn_dir / "cancel.json").read_text())
        self.assertEqual(command["processIdentity"]["pid"], 4242)

    def test_configured_model_is_not_validated_against_the_global_default(self):
        self.write_receipts(model=CONFIGURED_MODEL)
        with self.assertRaisesRegex(ValueError, "identity differs"):
            recovery.verify_failed_turn(self.sid, self.task_id, self.turn, self.cwd,
                                        OTHER_MODEL)
        with self.assertRaisesRegex(ValueError, "identity differs"):
            recovery._verify_pointer_identity(self.sid, self.task_id, self.turn, self.cwd,
                                              OTHER_MODEL)

    def test_cancellation_and_recovery_do_not_use_the_global_default_constant(self):
        for function in (opencode.cancel, recovery.verify_failed_turn,
                         recovery._verify_pointer_identity):
            source = inspect.getsource(function)
            self.assertNotIn("opencode.MODEL", source)
            self.assertNotIn("MODEL", source.replace("expected_model", "")
                             .replace("configured_model", ""))

    # -- fail-closed identity links --------------------------------------
    def test_recovery_identity_links_fail_closed(self):
        cases = {
            "turn": {"launch": {"turn": "other-turn"}},
            "taskId": {"launch": {"taskId": "other-task"}},
            "sessionId": {"launch": {"sessionId": self.other_sid}},
            "cwd": {"launch": {"cwd": str(self.other_cwd.resolve())}},
            "launch model": {"model": OTHER_MODEL},
            "turn state model": {"model": CONFIGURED_MODEL, "state_extra": {"model": OTHER_MODEL}},
            "turn state": {"state": "working"},
            "turn cleanup": {"cleanup": "uncertain"},
            "turn finishedAt": {"state_extra": {"finishedAt": "soon"}},
            "result state": {"result_extra": {"state": "working"}},
            "result cleanup": {"result_extra": {"cleanup": "uncertain"}},
            "result finishedAt": {"result_extra": {"finishedAt": "soon"}},
        }
        for name, override in cases.items():
            with self.subTest(link=name):
                self.write_receipts(model=override.pop("model", CONFIGURED_MODEL),
                                    **{key: value for key, value in override.items()
                                       if key not in ("model",)})
                with self.assertRaises(ValueError):
                    recovery.verify_failed_turn(self.sid, self.task_id, self.turn, self.cwd,
                                                CONFIGURED_MODEL)

    def test_recovery_rejects_incomplete_receipts(self):
        self.write_receipts()
        (self.data / "opencode" / self.sid / "turns" / self.turn / "result.json").unlink()
        with self.assertRaisesRegex(ValueError, "state/result is incomplete"):
            recovery.verify_failed_turn(self.sid, self.task_id, self.turn, self.cwd,
                                        CONFIGURED_MODEL)

    def test_recovery_rejects_an_invalid_tool_failure_list(self):
        self.write_receipts(tool_failures="not-a-list")
        with self.assertRaisesRegex(ValueError, "invalid tool-failure list"):
            recovery.verify_failed_turn(self.sid, self.task_id, self.turn, self.cwd,
                                        CONFIGURED_MODEL)

    def test_cancellation_identity_links_fail_closed(self):
        cases = {
            "launch sessionId": {"launch": {"sessionId": self.other_sid}},
            "launch cwd": {"launch": {"cwd": str(self.other_cwd.resolve())}},
            "launch model unapproved": {"model": "opencode/paid-model"},
            "turn model": {"model": CONFIGURED_MODEL, "state_extra": {"model": OTHER_MODEL}},
            "turn sessionId": {"state_extra": {"sessionId": self.other_sid}},
            "turn cwd": {"state_extra": {"cwd": str(self.other_cwd.resolve())}},
        }
        for name, override in cases.items():
            with self.subTest(link=name):
                model = override.pop("model", CONFIGURED_MODEL)
                state_extra = override.pop("state_extra", None)
                launch = override.pop("launch", None)
                turn_dir = self.write_working_turn(model=model, state_extra=state_extra)
                (turn_dir / "cancel.json").unlink(missing_ok=True)
                if launch is not None:
                    session = self.data / "opencode" / self.sid
                    receipt = {"turn": self.turn, "taskId": self.task_id,
                               "sessionId": self.sid, "cwd": self.cwd, "model": model}
                    receipt.update(launch)
                    (session / "current.json").write_text(json.dumps(receipt))
                with patch.object(opencode, "identity_matches", return_value=True):
                    with self.assertRaises(ValueError):
                        opencode.cancel(self.slot)
                self.assertFalse((turn_dir / "cancel.json").exists())

    def test_cancellation_refuses_an_unconfirmed_process_identity(self):
        turn_dir = self.write_working_turn()
        with patch.object(opencode, "identity_matches", return_value=False):
            with self.assertRaisesRegex(ValueError, "identity is not live"):
                opencode.cancel(self.slot)
        self.assertFalse((turn_dir / "cancel.json").exists())

    def test_cancellation_uses_the_inflight_model_after_the_next_selection_changes(self):
        self.write_working_turn()
        with patch.object(opencode, "identity_matches", return_value=True):
            result = opencode.cancel(dict(self.slot, model=OTHER_MODEL))
        self.assertEqual(result["turn"], self.turn)
        self.assertTrue((self.data / "opencode" / self.sid / "turns" / self.turn /
                         "cancel.json").exists())

    # -- rate limit and quota --------------------------------------------
    def test_rate_limit_and_quota_failures_remain_rejected(self):
        for text in ("Rate limit exceeded for this key",
                     "HTTP 429 too many requests",
                     "quota exceeded for the organization",
                     "insufficient credits on the account",
                     "403 forbidden"):
            with self.subTest(failure=text):
                self.write_receipts(error=text)
                with self.assertRaisesRegex(ValueError, "Rate-limit/quota"):
                    recovery.verify_failed_turn(self.sid, self.task_id, self.turn, self.cwd,
                                                CONFIGURED_MODEL)

    def test_rate_limit_in_a_tool_failure_remain_rejected(self):
        self.write_receipts(tool_failures=[{"error": "429 too many requests"}])
        with self.assertRaisesRegex(ValueError, "Rate-limit/quota"):
            recovery.verify_failed_turn(self.sid, self.task_id, self.turn, self.cwd,
                                        CONFIGURED_MODEL)

    def test_a_live_native_process_group_still_blocks_recovery(self):
        self.write_receipts(process_identity={"pid": 4242, "pgid": 4242, "start": [1],
                                              "executable": "/usr/bin/opencode"})
        with patch.object(opencode, "_group_exists", return_value=True):
            with self.assertRaisesRegex(ValueError, "process group is still live"):
                recovery.verify_failed_turn(self.sid, self.task_id, self.turn, self.cwd,
                                            CONFIGURED_MODEL)

    def test_an_invalid_native_process_identity_remain_rejected(self):
        self.write_receipts(process_identity={"pid": 4242})
        with self.assertRaisesRegex(ValueError, "no complete native process identity"):
            recovery.verify_failed_turn(self.sid, self.task_id, self.turn, self.cwd,
                                        CONFIGURED_MODEL)

    # -- no provider or model call ---------------------------------------
    def test_validation_makes_no_provider_or_model_call(self):
        self.write_receipts()
        calls = []

        def forbidden(*args, **kwargs):
            calls.append(args)
            raise AssertionError("the repaired path must not start a provider or model")

        with patch("subprocess.Popen", side_effect=forbidden), \
                patch("subprocess.run", side_effect=forbidden), \
                patch("subprocess.check_output", side_effect=forbidden), \
                patch.object(opencode, "identity_matches", return_value=False), \
                patch.object(opencode, "_group_exists", return_value=False):
            recovery.verify_failed_turn(self.sid, self.task_id, self.turn, self.cwd,
                                        CONFIGURED_MODEL)
            recovery._verify_pointer_identity(self.sid, self.task_id, self.turn, self.cwd,
                                              CONFIGURED_MODEL)
        # A live turn is cancelled afterwards, on its own receipt.
        self.write_working_turn()
        with patch("subprocess.Popen", side_effect=forbidden), \
                patch("subprocess.run", side_effect=forbidden), \
                patch("subprocess.check_output", side_effect=forbidden), \
                patch.object(opencode, "identity_matches", return_value=True):
            opencode.cancel(self.slot)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
