"""The complete recovery path for a configured approved free model.

A session configured with an explicitly approved free model must be able to
travel the whole path: authorization, dispatch, the managed launch and the
native receipt validation. Every identity link must still fail closed, the
default DeepSeek behaviour must stay pinned, and no paid model may launch.

Every provider, model and OpenCode CLI in this module is a fixture or a
fake. No process is ever started for real and no model is ever called.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import dispatch, opencode, paths, recovery, store  # noqa: E402

# The approved free model the fleet roster configures for this session.
CONFIGURED_MODEL = "opencode-go/minimax-m3.1-flash-free"
CONFIGURED_PROVIDER = "opencode-go"
CONFIGURED_MODEL_ID = "minimax-m3.1-flash-free"
# The fleet default, which must keep behaving exactly as before.
DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"
DEFAULT_PROVIDER = "opencode"
DEFAULT_MODEL_ID = "deepseek-v4.1-flash-free"
# A model that is neither approved nor free.
PAID_MODEL = "opencode/paid-premium-model"
REAL_POPEN = subprocess.Popen


def json_pipe(events):
    read_fd, write_fd = os.pipe()
    body = "".join(json.dumps(event) + "\n" for event in events).encode()
    os.write(write_fd, body)
    os.close(write_fd)
    return os.fdopen(read_fd, "rb", buffering=0)


class FakeProcess:
    """A stand-in for the OpenCode CLI. It never runs anything."""

    next_pid = 53001

    def __init__(self, *args, events=(), code=0, on_exit=None, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = type(self).next_pid
        type(self).next_pid += 1
        input_read, input_write = os.pipe()
        self.stdin_reader = os.fdopen(input_read, "rb", buffering=0)
        self.stdin = os.fdopen(input_write, "wb", buffering=0)
        self.stdout = json_pipe(events)
        self.returncode = code
        self.code = code
        self.terminated = False
        self.killed = False
        self.on_exit = on_exit
        self.on_exit_called = False

    def wait(self, timeout=None):
        if not self.on_exit_called and self.on_exit:
            self.on_exit_called = True
            self.on_exit()
        self.returncode = self.code
        if not self.stdin.closed:
            self.stdin.close()
        return self.code

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class RecoveryLaunchPathTests(unittest.TestCase):
    """Authorize, dispatch, launch and validate, offline and deterministically."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.native_cwd = self.root / "native-worker"
        self.native_cwd.mkdir()
        self.worktree = self.root / "owned-worktree"
        self.worktree.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "repair", str(self.worktree)], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "config", "user.email", "fleet@test"],
                       check=True)
        subprocess.run(["git", "-C", str(self.worktree), "config", "user.name", "Fleet Test"],
                       check=True)
        (self.worktree / "partial.txt").write_text("preserve this partial work\n")
        subprocess.run(["git", "-C", str(self.worktree), "add", "partial.txt"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "commit", "-qm", "fixture"], check=True)
        self.sid = "ses_recovery_launch_path"
        self.other_sid = "ses_recovery_launch_other"
        self.turn = "turn_failed_launch"
        self.failed_task = "failed-task"
        self.successor = "successor-task"
        self.native_db = self.root / "native.db"
        self._create_native_db(self.native_db, self.sid, self.native_cwd, CONFIGURED_MODEL)
        self.slot = {"sessionId": self.sid, "cwd": str(self.native_cwd.resolve()),
                     "sessionCwd": str(self.native_cwd.resolve()),
                     "databasePath": str(self.native_db), "model": CONFIGURED_MODEL,
                     "provider": "opencode", "dispatchEnabled": True}
        self.data = self.root / "data"
        self.queue = self.data / "queue.json"
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "fleet.sqlite"
        self.db = store.database(self.db_path)
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (self.failed_task, self.sid, str(self.native_cwd.resolve()),
                         json.dumps({"message": "owner branch repair worktree "
                                                + str(self.worktree.resolve()),
                                     "sessionId": self.sid,
                                     "rootAuthorized": True}), "finished", 1))
        self.db.commit()
        self.addCleanup(self.db.close)
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
    def _create_native_db(self, path, sid, cwd, model):
        provider, _, name = model.partition("/")
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, project_id TEXT, "
                       "model TEXT)")
            db.execute("INSERT INTO session_v2 VALUES (?,?,?,?)",
                       (sid, str(cwd), "project-test",
                        json.dumps({"id": name, "providerID": provider})))
            db.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, "
                       "seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")

    def prepare_failure(self, model=CONFIGURED_MODEL, state="error", cleanup="confirmed",
                        error="OpenCode turn did not produce a verified native completion",
                        tool_failures=None, process_identity=None, state_extra=None,
                        result_extra=None):
        session = self.data / "opencode" / self.sid
        turn_dir = session / "turns" / self.turn
        turn_dir.mkdir(parents=True, exist_ok=True)
        launch = {"turn": self.turn, "taskId": self.failed_task, "sessionId": self.sid,
                  "cwd": str(self.native_cwd.resolve()), "model": model}
        (session / "current.json").write_text(json.dumps(launch))
        turn_state = dict(launch, state=state, cleanup=cleanup, error=error, finishedAt=100)
        if state_extra:
            turn_state.update(state_extra)
        (turn_dir / "state.json").write_text(json.dumps(turn_state))
        result = dict(turn=self.turn, taskId=self.failed_task, sessionId=self.sid,
                      expectedModel=model,
                      cwd=str(self.native_cwd.resolve()), state=state, cleanup=cleanup,
                      error=error, toolFailures=[], finishedAt=100,
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

    def authorize(self, **overrides):
        snapshot = recovery.worktree_snapshot(str(self.worktree.resolve()))
        values = {"session_id": self.sid, "failed_task_id": self.failed_task,
                  "turn": self.turn, "recovery_id": "recovery-launch",
                  "successor_task_id": self.successor, "ownership_task_id": self.failed_task,
                  "owned_worktree": str(self.worktree.resolve()), "branch": snapshot["branch"],
                  "head": snapshot["head"], "worktree_sha256": snapshot["worktreeSha256"],
                  "note": "confirmed failed completion and preserved partial checkout"}
        values.update(overrides)
        return recovery.authorize_opencode(self.db, **values)

    def queue_successor(self, recovery_id="recovery-launch"):
        body = json.dumps({"id": self.successor, "previousTaskId": self.failed_task,
                           "recoveryId": recovery_id, "rootAuthorized": True,
                           "message": "continue"})
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (self.successor, self.sid, str(self.native_cwd.resolve()), body,
                         "queued", 1))
        self.db.commit()

    def dispatch_successor(self):
        real_popen = subprocess.Popen

        def fake_popen(*args, **kwargs):
            # Git fingerprinting still runs for real; the Fleet wrapper never does.
            if args and args[0] and args[0][0] == "git":
                return real_popen(*args, **kwargs)
            return Mock(pid=1234)

        with patch.object(opencode.subprocess, "Popen", side_effect=fake_popen):
            dispatch.dispatch(self.db, {self.sid: self.slot},
                              {self.sid: {"state": "error",
                                          "cwd": str(self.native_cwd.resolve())}},
                              dispatch.send)
        return self.db.execute("SELECT state FROM tasks WHERE id=?",
                                (self.successor,)).fetchone()[0]

    def written_request(self):
        session = self.data / "opencode" / self.sid
        launch = json.loads((session / "current.json").read_text())
        return session / "turns" / launch["turn"] / "request.json"

    def prepare_launch(self, request_path):
        """Stand in for the wrapper: take the live session lock it inherits."""
        lock = opencode._session_lock(request_path.parent.parent.parent)
        self.addCleanup(lock.close)
        request = json.loads(request_path.read_text())
        request["lockFd"] = lock.fileno()
        request_path.write_text(json.dumps(request))
        return lock

    def run_turn(self, request_path, provider=CONFIGURED_PROVIDER, model=CONFIGURED_MODEL_ID,
                 text="OK"):
        events = [
            {"type": "step_start", "timestamp": 1000, "id": "prt_start", "sessionID": self.sid,
             "messageID": "msg_1", "part": {"type": "step-start"}},
            {"type": "text", "timestamp": 1000, "id": "prt_text", "sessionID": self.sid,
             "messageID": "msg_1", "part": {"type": "text", "text": text}},
            {"type": "step_finish", "timestamp": 1000, "id": "prt_finish", "sessionID": self.sid,
             "messageID": "msg_1", "part": {"type": "step-finish", "reason": "stop"},
             "tokens": {"input": 4, "output": 1}},
        ]

        def persist_completion():
            state = json.loads((request_path.parent / "state.json").read_text())
            now = max(int(time.time() * 1000), int(state["startedAt"] * 1000) + 1)
            assistant = {"time": {"created": now, "streamed": now + 1, "completed": now + 2},
                         "model": {"id": model, "providerID": provider},
                         "finish": "stop", "rawFinish": "stop",
                         "content": [{"type": "text", "text": text}]}
            with sqlite3.connect(self.native_db) as db:
                db.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                           ("msg_user", self.sid, "user", 4, now - 1, now - 1,
                            json.dumps({"time": {"created": now - 1}, "text": "continue"})))
                db.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                           ("msg_1", self.sid, "assistant", 5, now, now + 2,
                            json.dumps(assistant)))
                db.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                           ("msg_idle", self.sid, "idle", 10, now + 3, now + 3,
                            json.dumps({"time": {"created": now + 3}, "outcome": "succeeded"})))

        def fake_popen(*args, **kwargs):
            return FakeProcess(*args, events=events, code=0,
                               on_exit=persist_completion, **kwargs)

        # The wrapper inherits the live session lock; the test stands in for it.
        self.prepare_launch(request_path)

        with patch.object(opencode.subprocess, "Popen", side_effect=fake_popen) as popen, \
                patch.object(opencode, "process_identity", side_effect=lambda pid: {
                    "pid": int(pid), "pgid": int(pid), "start": [1],
                    "executable": "/usr/bin/opencode"}), \
                patch.object(opencode, "_terminate_owned_group",
                             return_value={"confirmed": True, "uncertain": False}):
            result = opencode.run_turn(request_path)
        return result, popen

    # -- the complete path ------------------------------------------------
    def test_configured_free_model_travels_the_whole_recovery_path(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.assertEqual(self.authorize()["status"], "authorized")
        self.queue_successor()
        self.assertEqual(self.dispatch_successor(), "sent")

        request_path = self.written_request()
        request = json.loads(request_path.read_text())
        self.assertEqual(request["model"], CONFIGURED_MODEL)
        self.assertEqual(request["sessionId"], self.sid)
        self.assertEqual(request["cwd"], str(self.native_cwd.resolve()))
        self.assertEqual(request["sessionCwd"], str(self.native_cwd.resolve()))
        self.assertIn(str(self.worktree.resolve()), request["message"])
        self.assertEqual(request["ownedWorktree"], str(self.worktree.resolve()))
        self.assertEqual(request["branch"], "repair")

        result, popen = self.run_turn(request_path)
        self.assertEqual(result["state"], "done")
        retained = json.loads((request_path.parent / "result.json").read_text())
        state = json.loads((request_path.parent / "state.json").read_text())
        self.assertEqual(state["turn"], request_path.parent.name)
        self.assertNotIn("processIdentity", state)
        self.assertEqual(set(retained["managedProcessIdentity"]),
                         {"pid", "pgid", "start", "executable"})
        with patch.object(opencode, "identity_matches", return_value=False), \
                patch.object(opencode, "_group_exists", return_value=False):
            stopped, _ = recovery.verify_stopped_native_execution(self.slot, self.successor)
        self.assertTrue(stopped)
        self.assertEqual(result["actualProvider"], CONFIGURED_PROVIDER)
        self.assertEqual(result["actualModel"], CONFIGURED_MODEL_ID)
        args = popen.call_args.args[0]
        self.assertEqual(args[args.index("--model") + 1], CONFIGURED_MODEL)
        self.assertEqual(args[args.index("--session") + 1], self.sid)

    def test_the_isolated_runtime_pins_the_configured_model_and_its_provider(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        self.dispatch_successor()
        _, popen = self.run_turn(self.written_request())
        env = popen.call_args.kwargs["env"]
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["model"], CONFIGURED_MODEL)
        self.assertEqual(config["small_model"], CONFIGURED_MODEL)
        self.assertEqual(config["enabled_providers"], [CONFIGURED_PROVIDER])

    def test_the_native_session_cwd_and_identity_stay_fixed(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        self.dispatch_successor()
        request = json.loads(self.written_request().read_text())
        self.assertEqual(request["cwd"], str(self.native_cwd.resolve()))
        self.assertEqual(request["sessionCwd"], str(self.native_cwd.resolve()))
        native = opencode.verify_native_session(self.sid, str(self.native_cwd.resolve()),
                                               str(self.native_db))
        self.assertEqual(native["sessionCwd"], str(self.native_cwd.resolve()))
        self.assertEqual(native["nativeModelProvider"], CONFIGURED_PROVIDER)
        self.assertEqual(native["nativeModelId"], CONFIGURED_MODEL_ID)

    # -- identity boundaries fail closed ---------------------------------
    def test_a_request_for_another_session_cannot_launch(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        self.dispatch_successor()
        request_path = self.written_request()
        request = json.loads(request_path.read_text())
        request["sessionId"] = self.other_sid
        request_path.write_text(json.dumps(request))
        self.prepare_launch(request_path)
        with patch.object(opencode.subprocess, "Popen") as popen:
            with self.assertRaises(ValueError):
                opencode.run_turn(request_path)
        popen.assert_not_called()

    def test_a_failed_task_that_is_not_the_recorded_turn_cannot_be_recovered(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        ("other-failed-task", self.sid, str(self.native_cwd.resolve()),
                         json.dumps({"message": "another failed task"}), "finished", 1))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "identity differs from the requested"):
            self.authorize(failed_task_id="other-failed-task")

    def test_a_request_naming_another_turn_cannot_launch(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        self.dispatch_successor()
        request_path = self.written_request()
        request = json.loads(request_path.read_text())
        request["turn"] = "other-turn"
        request_path.write_text(json.dumps(request))
        self.prepare_launch(request_path)
        with patch.object(opencode.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "turn ID is invalid|"
                                            "does not match its turn directory"):
                opencode.run_turn(request_path)
        popen.assert_not_called()

    def test_a_request_for_another_cwd_cannot_launch(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        self.dispatch_successor()
        request_path = self.written_request()
        request = json.loads(request_path.read_text())
        request["cwd"] = str(self.worktree.resolve())
        request["sessionCwd"] = str(self.worktree.resolve())
        request_path.write_text(json.dumps(request))
        self.prepare_launch(request_path)
        with patch.object(opencode.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "sessionCwd does not match cwd|"
                                            "differs from configured cwd"):
                opencode.run_turn(request_path)
        popen.assert_not_called()

    def test_a_request_naming_another_model_cannot_launch(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        self.dispatch_successor()
        request_path = self.written_request()
        request = json.loads(request_path.read_text())
        request["model"] = DEFAULT_MODEL
        request_path.write_text(json.dumps(request))
        self.prepare_launch(request_path)
        with patch.object(opencode.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "differs from the native session model"):
                opencode.run_turn(request_path)
        popen.assert_not_called()

    def test_a_request_naming_an_unapproved_model_cannot_launch(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        self.dispatch_successor()
        request_path = self.written_request()
        request = json.loads(request_path.read_text())
        request["model"] = PAID_MODEL
        request_path.write_text(json.dumps(request))
        self.prepare_launch(request_path)
        with patch.object(opencode.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "approved free model"):
                opencode.run_turn(request_path)
        popen.assert_not_called()

    def test_an_unconfirmed_process_identity_is_not_a_live_turn(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        self.dispatch_successor()
        request_path = self.written_request()
        request = json.loads(request_path.read_text())
        request["model"] = CONFIGURED_MODEL
        request_path.write_text(json.dumps(request))
        with patch.object(opencode, "identity_matches", return_value=False):
            with self.assertRaises(ValueError):
                opencode.run_turn(request_path)

    # -- negatives --------------------------------------------------------
    def test_a_live_native_process_group_blocks_the_recovery(self):
        self.prepare_failure(model=CONFIGURED_MODEL,
                            process_identity={"pid": 4242, "pgid": 4242, "start": [1],
                                              "executable": "/usr/bin/opencode"})
        with patch.object(opencode, "_group_exists", return_value=True):
            with self.assertRaisesRegex(ValueError, "process group is still live"):
                self.authorize()

    def test_unconfirmed_cleanup_blocks_the_recovery(self):
        self.prepare_failure(model=CONFIGURED_MODEL, cleanup="uncertain")
        with self.assertRaisesRegex(ValueError, "cleanup is not verified"):
            self.authorize()

    def test_a_changed_owned_worktree_blocks_the_recovery(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        (self.worktree / "untracked.txt").write_text("changed after authorization\n")
        real_popen = subprocess.Popen

        def fake_popen(*args, **kwargs):
            if args and args[0] and args[0][0] == "git":
                return real_popen(*args, **kwargs)
            return Mock(pid=1234)

        with patch.object(opencode.subprocess, "Popen", side_effect=fake_popen):
            dispatch.dispatch(self.db, {self.sid: self.slot},
                              {self.sid: {"state": "error",
                                          "cwd": str(self.native_cwd.resolve())}},
                              dispatch.send)
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                          (self.successor,)).fetchone()[0], "queued")
        self.assertTrue(self.db.execute("SELECT 1 FROM events WHERE kind='needs_attention'"
                                        ).fetchone())

    def test_a_second_authorization_of_the_same_failed_turn_is_refused(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        with self.assertRaisesRegex(ValueError, "already has an active recovery record"):
            self.authorize(recovery_id="recovery-second", successor_task_id="successor-2")

    def test_rate_limit_and_quota_failures_are_refused(self):
        for text in ("Rate limit exceeded", "429 too many requests",
                     "quota exceeded", "insufficient credits"):
            with self.subTest(failure=text):
                self.prepare_failure(model=CONFIGURED_MODEL, error=text)
                with self.assertRaisesRegex(ValueError, "Rate-limit/quota"):
                    self.authorize()
                self.prepare_failure(model=CONFIGURED_MODEL)

    def test_a_task_cannot_supply_the_model(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        body = json.dumps({"id": self.successor, "previousTaskId": self.failed_task,
                           "recoveryId": "recovery-launch", "rootAuthorized": True,
                           "message": "continue", "model": PAID_MODEL})
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (self.successor, self.sid, str(self.native_cwd.resolve()), body,
                         "queued", 1))
        self.db.commit()
        self.dispatch_successor()
        request = json.loads(self.written_request().read_text())
        self.assertEqual(request["model"], CONFIGURED_MODEL)

    # -- regressions ------------------------------------------------------
    def test_the_default_deepseek_behaviour_stays_pinned(self):
        deepseek_db = self.root / "native-deepseek.db"
        self._create_native_db(deepseek_db, self.sid, self.native_cwd, DEFAULT_MODEL)
        self.slot["model"] = DEFAULT_MODEL
        self.slot["databasePath"] = str(deepseek_db)
        self.prepare_failure(model=DEFAULT_MODEL)
        self.authorize()
        self.queue_successor()
        self.dispatch_successor()
        request = json.loads(self.written_request().read_text())
        self.assertEqual(request["model"], DEFAULT_MODEL)
        _, popen = self.run_turn(self.written_request())
        env = popen.call_args.kwargs["env"]
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["model"], DEFAULT_MODEL)
        self.assertEqual(config["small_model"], DEFAULT_MODEL)
        self.assertEqual(config["enabled_providers"], [DEFAULT_PROVIDER])

    def test_no_paid_model_can_be_configured_or_launched(self):
        for model in (PAID_MODEL, "anthropic/claude-paid", "opencode/paid-premium-model"):
            with self.subTest(model=model):
                with self.assertRaisesRegex(ValueError, "approved free models"):
                    opencode.verify_slot(dict(self.slot, model=model))
                with self.assertRaisesRegex(ValueError, "approved free models"):
                    opencode.approved_free_model(dict(self.slot, model=model))

    def test_the_approved_free_model_list_is_explicit(self):
        self.assertIn(opencode.MODEL, opencode.APPROVED_FREE_MODELS)
        self.assertEqual(opencode.APPROVED_FREE_MODELS[0], opencode.MODEL)
        self.assertIn(CONFIGURED_MODEL, opencode.APPROVED_FREE_MODELS)

    def test_a_native_completion_naming_another_model_is_refused(self):
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        self.dispatch_successor()
        request_path = self.written_request()
        # The native completion is the native evidence: it must report the model
        # the request asked for, so a completion that names another model is a
        # fallback and never a success.
        with patch.object(opencode, "process_identity", side_effect=lambda pid: {
                "pid": int(pid), "pgid": int(pid), "start": [1],
                "executable": "/usr/bin/opencode"}), \
                patch.object(opencode, "_terminate_owned_group",
                             return_value={"confirmed": True, "uncertain": False}):
            result, _ = self.run_turn(request_path, model="some-other-model")
        self.assertNotEqual(result["state"], "done")

    def test_a_native_session_recording_another_provider_cannot_launch(self):
        # Same model id, different provider. The native identity carries both,
        # so the provider must match as well as the model id.
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        self.dispatch_successor()
        request_path = self.written_request()
        self.prepare_launch(request_path)
        with sqlite3.connect(self.native_db) as db:
            db.execute("UPDATE session_v2 SET model=? WHERE id=?",
                       (json.dumps({"id": CONFIGURED_MODEL_ID, "providerID": "opencode"}),
                        self.sid))
        with patch.object(opencode.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "differs from the native session model"):
                opencode.run_turn(request_path)
        popen.assert_not_called()

    def test_a_request_naming_another_approved_model_cannot_launch(self):
        # The native session records the model and provider it runs. A request
        # that names a different approved model is still a mismatch against that
        # native identity and is refused before any turn starts.
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        self.dispatch_successor()
        request_path = self.written_request()
        request = json.loads(request_path.read_text())
        request["model"] = DEFAULT_MODEL
        request_path.write_text(json.dumps(request))
        self.prepare_launch(request_path)
        with patch.object(opencode.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "differs from the native session model"):
                opencode.run_turn(request_path)
        popen.assert_not_called()

    def test_a_slot_whose_configured_model_differs_from_the_native_session_is_refused(self):
        # The configured model and provider of the target slot must match the
        # current native session identity, so a slot that drifted is refused at
        # the managed launch rather than starting a turn.
        self.prepare_failure(model=CONFIGURED_MODEL)
        self.authorize()
        self.queue_successor()
        record = recovery.claim_recovery(self.db, "recovery-launch", self.successor,
                                         self.sid, self.failed_task)
        self.db.commit()
        self.assertIsNotNone(record)
        with sqlite3.connect(self.native_db) as db:
            db.execute("UPDATE session_v2 SET model=? WHERE id=?",
                       (json.dumps({"id": CONFIGURED_MODEL_ID, "providerID": "opencode"}),
                        self.sid))
        def git_then_fake(*args, **kwargs):
            if args and args[0] and args[0][0] == "git":
                return REAL_POPEN(*args, **kwargs)
            return Mock(pid=1234)

        with patch.object(opencode.subprocess, "Popen", side_effect=git_then_fake) as popen:
            with self.assertRaisesRegex(ValueError, "differs from the native session model"):
                opencode.send(self.slot, "continue", self.successor, record)
        self.assertEqual([call for call in popen.call_args_list
                          if call.args and call.args[0] and call.args[0][0] != "git"], [])


if __name__ == "__main__":
    unittest.main()
