"""OpenCode adapter safety and completion checks; no live model calls."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import dispatch, opencode, paths, store  # noqa: E402


class OpenCodeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cwd = root / "worktree"
        self.cwd.mkdir()
        self.sid = "ses_01J9TESTSESSION"
        self.slot = {"sessionId": self.sid, "cwd": str(self.cwd),
                     "sessionCwd": str(self.cwd), "model": opencode.MODEL,
                     "provider": "opencode", "dispatchEnabled": True,
                     "nativeSessionVerified": True, "sessionExclusiveVerified": True,
                     "auxiliaryModelsVerified": True, "freeCanaryVerified": True}
        self.data = root / "data" / "opencode"
        self.logs = root / "logs"
        self.db = store.database(root / "fleet.sqlite")
        self.addCleanup(self.db.close)
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("OPENCODE", self.data), ("LOGS", self.logs),
                            ("BIN", root / "bin" / "fleet")):
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        exe = patch.object(paths, "executable", return_value="/usr/bin/opencode")
        exe.start()
        self.addCleanup(exe.stop)

    def add_task(self, task_id="t1"):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (task_id, self.sid, str(self.cwd),
                         json.dumps({"message": "tiny task"}), "queued", 1))
        self.db.commit()

    def test_verifier_requires_standard_free_model_and_matching_session_cwd(self):
        self.assertEqual(opencode.verify_slot(self.slot)["model"], opencode.MODEL)
        for model in ("opencode-go/deepseek-v4.1-flash", "other/provider-model"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                opencode.verify_slot(dict(self.slot, model=model))
        with self.assertRaisesRegex(ValueError, "sessionCwd"):
            opencode.verify_slot(dict(self.slot, sessionCwd=str(self.cwd.parent)))
        with self.assertRaisesRegex(ValueError, "freeCanaryVerified"):
            opencode.verify_slot(dict(self.slot, freeCanaryVerified=False))

    def test_dispatch_requires_explicit_enablement_and_correlates_task_id(self):
        self.add_task()
        slots = {self.sid: dict(self.slot, dispatchEnabled=False)}
        states = {self.sid: {"state": "idle", "cwd": str(self.cwd)}}
        sent = []
        dispatch.dispatch(self.db, slots, states,
                          lambda *args, **kwargs: sent.append((args, kwargs)))
        self.assertEqual(sent, [])
        self.assertEqual(self.db.execute("SELECT state FROM tasks").fetchone()[0], "queued")
        slots[self.sid]["dispatchEnabled"] = True
        dispatch.dispatch(self.db, slots, states,
                          lambda *args, **kwargs: sent.append((args, kwargs)))
        self.assertEqual(sent[0][0], (slots[self.sid], "tiny task"))
        self.assertEqual(sent[0][1], {"task_id": "t1"})

    def test_cancel_uses_current_process_group_without_new_dispatch_gates(self):
        turn = "cancel-turn"
        root = self.data / self.sid
        turn_path = root / "turns" / turn
        turn_path.mkdir(parents=True)
        (root / "current.json").write_text(json.dumps({"turn": turn, "sessionId": self.sid,
                                                        "cwd": str(self.cwd), "model": opencode.MODEL}))
        (turn_path / "state.json").write_text(json.dumps({"sessionId": self.sid,
                                                           "cwd": str(self.cwd), "model": opencode.MODEL,
                                                           "state": "working", "pid": 98765}))
        with patch.object(opencode.os, "killpg") as killpg:
            result = opencode.cancel({"sessionId": self.sid, "cwd": str(self.cwd),
                                      "model": opencode.MODEL})
        killpg.assert_called_once_with(98765, opencode.signal.SIGINT)
        self.assertEqual(result, {"interrupted": 98765})

    def test_run_turn_requires_actual_pinned_model_and_terminal_success(self):
        turn = "turn-1"
        request_dir = self.data / self.sid / "turns" / turn
        request_dir.mkdir(parents=True)
        log_dir = self.logs / "opencode" / self.sid / turn
        request = {"turn": turn, "taskId": "t1", "sessionId": self.sid,
                   "cwd": str(self.cwd), "sessionCwd": str(self.cwd),
                   "model": opencode.MODEL, "message": "tiny task",
                   "executable": "/usr/bin/opencode", "logDir": str(log_dir)}
        (request_dir / "request.json").write_text(json.dumps(request))
        events = [
            {"type": "step_start", "sessionID": self.sid,
             "part": {"providerID": "opencode", "modelID": "deepseek-v4.1-flash-free"}},
            {"type": "text", "sessionID": self.sid,
             "part": {"type": "text", "text": "OK"}},
            {"type": "step_finish", "sessionID": self.sid,
             "part": {"type": "step-finish", "reason": "stop",
                      "tokens": {"input": 4, "output": 1}}},
        ]

        class FakeProcess:
            def __init__(self, *args, **kwargs):
                self.pid = 12345
                self.stdin = io.StringIO()
                self.stdout = io.StringIO("\n".join(json.dumps(event) for event in events) + "\n")
                self.returncode = 0
            def wait(self, timeout=None):
                return 0
            def poll(self):
                return 0

        with patch.object(opencode.subprocess, "Popen", side_effect=FakeProcess) as popen:
            result = opencode.run_turn(request_dir / "request.json")
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["actualProvider"], "opencode")
        self.assertEqual(result["actualModel"], "deepseek-v4.1-flash-free")
        args = popen.call_args.args[0]
        self.assertIn("--standalone", args)
        self.assertEqual(args[args.index("--model") + 1], opencode.MODEL)
        self.assertEqual(args[args.index("--session") + 1], self.sid)
        self.assertEqual(json.loads((request_dir / "result.json").read_text())["usage"],
                         {"input": 4, "output": 1})

    def test_run_turn_blocks_provider_mismatch_even_on_zero_exit(self):
        turn = "turn-2"
        request_dir = self.data / self.sid / "turns" / turn
        request_dir.mkdir(parents=True)
        request = {"turn": turn, "taskId": "t2", "sessionId": self.sid,
                   "cwd": str(self.cwd), "sessionCwd": str(self.cwd),
                   "model": opencode.MODEL, "message": "tiny task",
                   "executable": "/usr/bin/opencode", "logDir": str(self.logs / "turn-2")}
        (request_dir / "request.json").write_text(json.dumps(request))
        events = [
            {"type": "step_start", "sessionID": self.sid,
             "part": {"providerID": "opencode-go", "modelID": "deepseek-v4.1-flash"}},
            {"type": "text", "sessionID": self.sid, "part": {"text": "unexpected"}},
            {"type": "step_finish", "sessionID": self.sid,
             "part": {"reason": "stop", "tokens": {"input": 1, "output": 1}}},
        ]

        class FakeProcess:
            def __init__(self, *args, **kwargs):
                self.pid = 12346
                self.stdin = io.StringIO()
                self.stdout = io.StringIO("\n".join(json.dumps(event) for event in events) + "\n")
            def wait(self, timeout=None):
                return 0
            def poll(self):
                return 0

        with patch.object(opencode.subprocess, "Popen", side_effect=FakeProcess):
            result = opencode.run_turn(request_dir / "request.json")
        self.assertEqual(result["state"], "blocked")
        self.assertIn("provider mismatch", result["error"])

    def test_v205_json_can_confirm_session_and_usage_but_not_actual_model(self):
        turn = "turn-no-model-metadata"
        request_dir = self.data / self.sid / "turns" / turn
        request_dir.mkdir(parents=True)
        request = {"turn": turn, "taskId": "t3", "sessionId": self.sid,
                   "cwd": str(self.cwd), "sessionCwd": str(self.cwd),
                   "model": opencode.MODEL, "message": "tiny task",
                   "executable": "/usr/bin/opencode", "logDir": str(self.logs / turn)}
        (request_dir / "request.json").write_text(json.dumps(request))
        events = [
            {"type": "step_start", "part": {"sessionID": self.sid, "type": "step-start"}},
            {"type": "text", "part": {"sessionID": self.sid, "type": "text", "text": "OK"}},
            {"type": "step_finish", "part": {"sessionID": self.sid, "type": "step-finish",
                                                    "reason": "stop", "tokens": {"input": 4, "output": 1}}},
        ]

        class FakeProcess:
            def __init__(self, *args, **kwargs):
                self.pid = 12347
                self.stdin = io.StringIO()
                self.stdout = io.StringIO("\n".join(json.dumps(event) for event in events) + "\n")
            def wait(self, timeout=None):
                return 0
            def poll(self):
                return 0

        with patch.object(opencode.subprocess, "Popen", side_effect=FakeProcess):
            result = opencode.run_turn(request_dir / "request.json")
        saved = json.loads((request_dir / "result.json").read_text())
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["requestedModel"], opencode.MODEL)
        self.assertIsNone(result["actualModel"])
        self.assertIsNone(saved["model"])
        self.assertEqual(saved["expectedModel"], opencode.MODEL)

    def test_permission_request_is_blocked_even_if_cli_exits_zero(self):
        turn = "permission-turn"
        request_dir = self.data / self.sid / "turns" / turn
        request_dir.mkdir(parents=True)
        request = {"turn": turn, "taskId": "t4", "sessionId": self.sid,
                   "cwd": str(self.cwd), "sessionCwd": str(self.cwd),
                   "model": opencode.MODEL, "message": "edit a file",
                   "executable": "/usr/bin/opencode", "logDir": str(self.logs / turn)}
        (request_dir / "request.json").write_text(json.dumps(request))
        events = [
            {"type": "permission.asked", "data": {"sessionID": self.sid, "action": "edit"}},
            {"type": "text", "part": {"sessionID": self.sid, "type": "text", "text": "need permission"}},
            {"type": "step_finish", "part": {"sessionID": self.sid, "reason": "stop"}},
        ]

        class FakeProcess:
            def __init__(self, *args, **kwargs):
                self.pid = 12348
                self.stdin = io.StringIO()
                self.stdout = io.StringIO("\n".join(json.dumps(event) for event in events) + "\n")
            def wait(self, timeout=None):
                return 0
            def poll(self):
                return 0

        with patch.object(opencode.subprocess, "Popen", side_effect=FakeProcess):
            result = opencode.run_turn(request_dir / "request.json")
        self.assertEqual(result["state"], "blocked")
        self.assertIn("permission", result["error"])


if __name__ == "__main__":
    unittest.main()
