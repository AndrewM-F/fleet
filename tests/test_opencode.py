"""OpenCode v2 adapter checks using native v2.0.5 NDJSON event shapes."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import dispatch, manager, opencode, paths, store  # noqa: E402


def json_pipe(events):
    read_fd, write_fd = os.pipe()
    body = "".join(json.dumps(event) + "\n" for event in events).encode()
    os.write(write_fd, body)
    os.close(write_fd)
    return os.fdopen(read_fd, "rb", buffering=0)


class FakeProcess:
    next_pid = 43001

    def __init__(self, *args, events=(), code=0, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = self.next_pid
        type(self).next_pid += 1
        input_read, input_write = os.pipe()
        self.stdin_reader = os.fdopen(input_read, "rb", buffering=0)
        self.stdin = os.fdopen(input_write, "wb", buffering=0)
        self.stdout = json_pipe(events)
        self.returncode = code
        self.code = code
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        self.returncode = self.code
        if not self.stdin.closed:
            self.stdin.close()
        if not self.stdin_reader.closed:
            self.stdin_reader.close()
        return self.code

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class OpenCodeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cwd = root / "worktree"
        self.cwd.mkdir()
        self.sid = "ses_01J9TESTSESSION"
        self.native_db = root / "native" / "opencode.db"
        self.native_db.parent.mkdir()
        with sqlite3.connect(self.native_db) as db:
            db.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, project_id TEXT, model TEXT)")
            db.execute("INSERT INTO session_v2 VALUES (?,?,?,?)",
                       (self.sid, str(self.cwd), "project-test", opencode.MODEL))
        self.slot = {"sessionId": self.sid, "cwd": str(self.cwd),
                     "sessionCwd": str(self.cwd), "databasePath": str(self.native_db),
                     "model": opencode.MODEL, "provider": "opencode",
                     "dispatchEnabled": True}
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
        self.identity = {"pid": 43001, "pgid": 43001, "start": [100, 200],
                         "executable": "/usr/bin/opencode"}

    def add_task(self, task_id="t1"):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (task_id, self.sid, str(self.cwd),
                         json.dumps({"message": "tiny task"}), "queued", 1))
        self.db.commit()

    def request(self, turn="turn-1", task_id="t1", sid=None, message="tiny task",
                executable="/usr/bin/opencode", timeout_seconds=0):
        request_dir = self.data / (sid or self.sid) / "turns" / turn
        request_dir.mkdir(parents=True, exist_ok=True)
        lock = opencode._session_lock(request_dir.parent.parent)
        self.addCleanup(lock.close)
        log_dir = self.logs / "opencode" / (sid or self.sid) / turn
        request = {"turn": turn, "taskId": task_id, "sessionId": sid or self.sid,
                   "cwd": str(self.cwd.resolve()), "sessionCwd": str(self.cwd.resolve()),
                   "databasePath": str(self.native_db), "model": opencode.MODEL,
                   "message": message, "executable": os.path.realpath(executable),
                   "logDir": str(log_dir), "timeoutSeconds": timeout_seconds,
                   "lockFd": lock.fileno()}
        request_path = request_dir / "request.json"
        request_path.write_text(json.dumps(request))
        return request_path

    def install_hanging_cli(self):
        script = self.cwd / "run"
        self.leader_pid_path = self.cwd / "leader.pid"
        self.descendant_pid_path = self.cwd / "descendant.pid"
        script.write_text(
            "#!/bin/sh\n"
            "trap 'kill -TERM \"$child\" 2>/dev/null || true; wait \"$child\" 2>/dev/null || true; exit 0' TERM INT\n"
            "sleep 60 &\n"
            "child=$!\n"
            f"echo $$ > {str(self.leader_pid_path)!r}\n"
            f"echo $child > {str(self.descendant_pid_path)!r}\n"
            "printf '{\"type\":\"partial\"'\n"
            "while :; do sleep 1; done\n"
        )
        script.chmod(0o700)
        return os.path.realpath("/bin/bash")

    def wait_for_file(self, path, timeout=3):
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), f"timed out waiting for {path.name}")

    def write_current_pointer(self, request_path):
        request = json.loads(request_path.read_text())
        (request_path.parent.parent.parent / "current.json").write_text(json.dumps({
            "turn": request["turn"], "sessionId": request["sessionId"],
            "cwd": request["cwd"], "model": request["model"],
            "startedAt": time.time()}))

    def event(self, kind, part=None, **top):
        event = {"type": kind, "timestamp": 1000, "sessionID": self.sid}
        if part is not None:
            event["part"] = part
        event.update(top)
        return event

    def run_events(self, request_path, events, code=0):
        def fake_popen(*args, **kwargs):
            return FakeProcess(*args, events=events, code=code, **kwargs)
        def fake_identity(pid):
            return dict(self.identity, pid=int(pid), pgid=int(pid))
        with patch.object(opencode.subprocess, "Popen", side_effect=fake_popen) as popen, \
                patch.object(opencode, "process_identity", side_effect=fake_identity), \
                patch.object(opencode, "_terminate_owned_group",
                             return_value={"confirmed": True, "uncertain": False}) as terminate:
            result = opencode.run_turn(request_path)
        return result, popen, terminate

    def test_slot_verifies_paths_model_and_live_identity_without_attestation_flags(self):
        # Stale verification booleans are not trusted or required; launch repeats the DB check.
        slot = dict(self.slot, nativeSessionVerified=False, auxiliaryModelsVerified=False)
        self.assertEqual(opencode.verify_slot(slot)["model"], opencode.MODEL)
        for model in ("opencode-go/deepseek-v4.1-flash", "other/provider-model"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                opencode.verify_slot(dict(self.slot, model=model))
        with self.assertRaisesRegex(ValueError, "sessionCwd"):
            opencode.verify_slot(dict(self.slot, sessionCwd=str(self.cwd.parent)))
        with self.assertRaisesRegex(ValueError, "Claude mode"):
            opencode.verify_slot(dict(self.slot, mode="print"))

    def test_native_session_lookup_is_exact_read_only_and_checks_directory(self):
        result = opencode.verify_native_session(self.sid, self.cwd, self.native_db)
        self.assertEqual(result["sessionCwd"], str(self.cwd.resolve()))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            opencode.verify_native_session("ses_unknown", self.cwd, self.native_db)
        with self.assertRaisesRegex(ValueError, "differs"):
            opencode.verify_native_session(self.sid, self.cwd.parent, self.native_db)

    def test_unknown_session_fails_before_cli_can_auto_create(self):
        request_path = self.request(sid="ses_unknown")
        with patch.object(opencode.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "refusing to let the CLI create it"):
                opencode.run_turn(request_path)
        popen.assert_not_called()

    def test_dispatch_requires_enablement_and_correlates_task_id(self):
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

    def test_opencode_print_mode_never_routes_through_claude(self):
        slot = dict(self.slot, mode="print")
        with patch("fleet.claude.send_print") as claude_send, \
                patch("fleet.opencode.send") as opencode_send:
            with self.assertRaisesRegex(ValueError, "Claude mode"):
                dispatch.send(slot, "message", task_id="t1")
        claude_send.assert_not_called()
        opencode_send.assert_not_called()

    def test_claude_print_mode_routing_is_unchanged(self):
        slot = {"provider": "claude", "mode": "print"}
        with patch("fleet.claude.send_print") as send_print:
            dispatch.send(slot, "message")
        send_print.assert_called_once_with(slot, "message")

    def test_dispatch_lock_prevents_concurrent_session_turns(self):
        directory = self.data / self.sid
        held = opencode._session_lock(directory)
        try:
            with self.assertRaisesRegex(RuntimeError, "already has a Fleet turn"):
                opencode.send(self.slot, "tiny task", "t1")
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()

    def test_send_holds_lock_for_wrapper_and_writes_current_pointer(self):
        script_path = Path(paths.BIN)
        script_path.parent.mkdir(parents=True, exist_ok=True)
        source_root = Path(opencode.__file__).resolve().parents[1]
        script_path.write_text(
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(source_root)!r})\n"
            "from fleet.opencode import _verify_inherited_lock\n"
            "request_path = Path(sys.argv[2])\n"
            "request = json.loads(request_path.read_text())\n"
            "session_dir = request_path.parent.parent.parent\n"
            "_verify_inherited_lock(request['lockFd'], session_dir / 'dispatch.lock')\n"
            "(request_path.parent / 'wrapper-ready').write_text('ready')\n"
            "deadline = time.monotonic() + 5\n"
            "while not (request_path.parent / 'wrapper-release').exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "sys.exit(0 if (request_path.parent / 'wrapper-release').exists() else 3)\n"
        )
        real_popen = subprocess.Popen
        children = []

        def launch_child(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        with patch.object(opencode.subprocess, "Popen", side_effect=launch_child) as popen:
            opencode.send(self.slot, "tiny task", "t1")
        current = json.loads((self.data / self.sid / "current.json").read_text())
        self.assertEqual(current["sessionId"], self.sid)
        self.assertEqual(current["wrapperPid"], children[0].pid)
        self.assertEqual(len(popen.call_args.kwargs["pass_fds"]), 1)
        request_path = self.data / self.sid / "turns" / current["turn"] / "request.json"
        request = json.loads(request_path.read_text())
        self.assertEqual(popen.call_args.kwargs["pass_fds"], (request["lockFd"],))
        with self.assertRaisesRegex(RuntimeError, "already has a Fleet turn"):
            opencode._session_lock(self.data / self.sid)
        ready_path = request_path.parent / "wrapper-ready"
        deadline = time.monotonic() + 3
        while not ready_path.exists() and children[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready_path.exists(), "wrapper did not verify the inherited lock")
        with self.assertRaisesRegex(RuntimeError, "already has a Fleet turn"):
            opencode._session_lock(self.data / self.sid)
        (request_path.parent / "wrapper-release").write_text("release")
        self.assertEqual(children[0].wait(timeout=5), 0)
        with opencode._session_lock(self.data / self.sid):
            pass

    def test_launching_snapshot_does_not_make_sent_task_uncertain(self):
        turn = "launching-turn"
        turn_path = self.data / self.sid / "turns" / turn
        turn_path.mkdir(parents=True)
        started = time.time()
        (self.data / self.sid / "current.json").write_text(json.dumps({
            "turn": turn, "sessionId": self.sid, "cwd": str(self.cwd.resolve()),
            "model": opencode.MODEL, "startedAt": started}))
        (turn_path / "state.json").write_text(json.dumps({
            "turn": turn, "sessionId": self.sid, "cwd": str(self.cwd.resolve()),
            "model": opencode.MODEL, "state": "launching", "progress": 0,
            "startedAt": started}))
        self.add_task()
        self.db.execute("UPDATE tasks SET state='sent',body=? WHERE id='t1'",
                        (json.dumps({"baselineProgress": None}),))
        self.db.commit()

        observed = opencode.state(self.slot)
        self.assertEqual(observed["state"], "working")
        manager.observe(self.db, self.sid, observed, now=started + 1)
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id='t1'").fetchone()[0],
                         "running")
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM events WHERE kind='task_unconsumed'").fetchone()[0], 0)

    def test_partial_output_cancel_is_responsive_and_reaps_descendant_group(self):
        executable = self.install_hanging_cli()
        request_path = self.request(turn="cancel-partial", executable=executable)
        self.write_current_pointer(request_path)
        result_box = []
        errors = []

        def run():
            try:
                result_box.append(opencode.run_turn(request_path))
            except Exception as error:  # surface thread errors in the test thread
                errors.append(error)

        runner = threading.Thread(target=run, daemon=True)
        runner.start()
        state_path = request_path.parent / "state.json"
        deadline = time.monotonic() + 3
        process_identity = None
        observed_progress = 0
        while time.monotonic() < deadline:
            if state_path.exists():
                saved = json.loads(state_path.read_text())
                process_identity = saved.get("processIdentity")
                observed_progress = saved.get("progress", 0)
                if process_identity and observed_progress > 0:
                    break
            time.sleep(0.01)
        self.assertIsNotNone(process_identity, "native process identity was not recorded")
        self.assertGreater(observed_progress, 0, "partial output was not read before cancellation")
        self.assertTrue(opencode.identity_matches(process_identity),
                        f"process identity mismatch: {process_identity!r} != "
                        f"{opencode.process_identity(process_identity['pid'])!r}")

        disabled_slot = {"sessionId": self.sid, "cwd": str(self.cwd),
                         "model": opencode.MODEL, "dispatchEnabled": False}
        with patch.object(paths, "executable", side_effect=AssertionError("cancel resolved CLI")), \
                patch.object(opencode, "verify_slot", side_effect=AssertionError("cancel revalidated dispatch")):
            response = opencode.cancel(disabled_slot)
        self.assertEqual(response["turn"], "cancel-partial")
        runner.join(timeout=5)
        self.assertFalse(runner.is_alive(), "cancel remained blocked on a partial NDJSON line")
        self.assertEqual(errors, [])
        self.assertEqual(result_box[0]["state"], "stopped")
        self.assertEqual(result_box[0]["cleanup"], "confirmed")
        leader_pid = int(self.leader_pid_path.read_text())
        self.assertTrue(self.descendant_pid_path.exists())
        self.assertFalse(opencode._group_exists(leader_pid), "cancel left the subprocess group alive")

    def test_partial_output_timeout_is_responsive_and_reaps_descendant_group(self):
        executable = self.install_hanging_cli()
        request_path = self.request(turn="timeout-partial", executable=executable,
                                    timeout_seconds=1)
        started = time.monotonic()
        result = opencode.run_turn(request_path)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5, "timeout remained blocked on a partial NDJSON line")
        self.assertEqual(result["state"], "error")
        self.assertIn("cleanup confirmed", result["error"])
        self.assertEqual(result["cleanup"], "confirmed")
        leader_pid = int(self.leader_pid_path.read_text())
        self.assertTrue(self.descendant_pid_path.exists())
        self.assertFalse(opencode._group_exists(leader_pid), "timeout left the subprocess group alive")

    def test_timeout_remains_responsive_when_cli_does_not_read_large_stdin(self):
        executable = self.install_hanging_cli()
        request_path = self.request(turn="blocked-stdin", executable=executable,
                                    message="x" * (2 * 1024 * 1024), timeout_seconds=1)
        started = time.monotonic()
        result = opencode.run_turn(request_path)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5, "timeout remained blocked writing the task message")
        self.assertEqual(result["state"], "error")
        self.assertIn("cleanup confirmed", result["error"])
        self.assertEqual(result["cleanup"], "confirmed")
        leader_pid = int(self.leader_pid_path.read_text())
        self.assertTrue(self.descendant_pid_path.exists())
        self.assertFalse(opencode._group_exists(leader_pid), "timeout left the subprocess group alive")

    def test_v205_success_uses_only_documented_session_paths_and_pins_runtime(self):
        request_path = self.request()
        events = [
            self.event("step_start", {"id": "prt_start", "sessionID": self.sid,
                                       "messageID": "msg_1", "type": "step-start"}),
            self.event("text", {"id": "prt_text", "sessionID": self.sid,
                                 "type": "text", "text": "OK"}),
            self.event("step_finish", {"id": "prt_finish", "sessionID": self.sid,
                                        "type": "step-finish", "reason": "stop",
                                        "tokens": {"input": 4, "output": 1}}),
        ]
        result, popen, _ = self.run_events(request_path, events)
        self.assertEqual(result["state"], "done")
        self.assertIsNone(result["actualProvider"])
        self.assertIsNone(result["actualModel"])
        args = popen.call_args.args[0]
        self.assertIn("--standalone", args)
        self.assertEqual(args[args.index("--model") + 1], opencode.MODEL)
        self.assertEqual(args[args.index("--session") + 1], self.sid)
        self.assertEqual(args[args.index("--title") + 1], "Fleet task t1")
        self.assertNotIn("--auto", args)
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["OPENCODE_DB"], str(self.native_db.resolve()))
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["model"], opencode.MODEL)
        self.assertEqual(config["small_model"], opencode.MODEL)
        self.assertEqual(config["enabled_providers"], ["opencode"])
        self.assertEqual(config["plugins"], [])
        self.assertEqual(env["OPENCODE_CONFIG_PROJECT_DISABLE"], "1")
        self.assertNotIn("FLEET_TEST_AMBIENT", env)
        saved = json.loads((request_path.parent / "result.json").read_text())
        self.assertEqual(saved["usage"], {"input": 4, "output": 1})

    def test_transport_ignores_tool_payload_identity_lookalikes(self):
        request_path = self.request()
        events = [
            self.event("step_start", {"sessionID": self.sid, "type": "step-start"}),
            self.event("tool_use", {"sessionID": self.sid, "type": "tool", "tool": "shell",
                                     "state": {"status": "completed", "input": {
                                         "cwd": str(self.cwd.parent), "sessionID": "ses_wrong",
                                         "modelID": "deepseek-v4.1-flash"}}}),
            self.event("text", {"sessionID": self.sid, "type": "text", "text": "OK"}),
            self.event("step_finish", {"sessionID": self.sid, "type": "step-finish",
                                        "reason": "stop"}),
        ]
        result, _, _ = self.run_events(request_path, events)
        self.assertEqual(result["state"], "done")

    def test_direct_transport_provider_model_mismatch_stops_owned_turn(self):
        request_path = self.request(turn="wrong-model")
        events = [
            self.event("step_start", {"sessionID": self.sid, "type": "step-start",
                                       "providerID": "opencode-go",
                                       "modelID": "deepseek-v4.1-flash"}),
            self.event("text", {"sessionID": self.sid, "type": "text", "text": "unexpected"}),
        ]
        result, _, terminate = self.run_events(request_path, events)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("provider mismatch", result["error"])
        terminate.assert_called_once()

    def test_v205_error_event_after_stop_latches_failure_and_stops_turn(self):
        request_path = self.request(turn="error-after-stop")
        events = [
            self.event("step_start", {"sessionID": self.sid, "type": "step-start"}),
            self.event("text", {"sessionID": self.sid, "type": "text", "text": "partial"}),
            self.event("step_finish", {"sessionID": self.sid, "type": "step-finish",
                                        "reason": "stop"}),
            self.event("step_start", {"sessionID": self.sid, "type": "step-start"}),
            self.event("error", error={"type": "unknown", "message": "transport failed"}),
        ]
        result, _, terminate = self.run_events(request_path, events, code=0)
        self.assertEqual(result["state"], "error")
        self.assertIn("transport failed", result["error"])
        terminate.assert_called_once()

    def test_recoverable_tool_failure_is_retained_but_final_retest_can_succeed(self):
        request_path = self.request(turn="repaired-tool-error")
        events = [
            self.event("step_start", {"sessionID": self.sid, "type": "step-start"}),
            self.event("tool_use", {"partID": "prt_tool", "sessionID": self.sid,
                                     "messageID": "msg_1", "type": "tool", "tool": "shell",
                                     "id": "call_1", "state": {"status": "error",
                                     "input": {"command": "pytest"},
                                     "error": "Command exited with code 1"}}),
            self.event("step_finish", {"sessionID": self.sid, "type": "step-finish",
                                        "reason": "tool-calls"}),
            self.event("step_start", {"sessionID": self.sid, "type": "step-start"}),
            self.event("tool_use", {"partID": "prt_tool_2", "sessionID": self.sid,
                                     "type": "tool", "tool": "shell",
                                     "state": {"status": "completed", "input": {"command": "pytest"}}}),
            self.event("text", {"sessionID": self.sid, "type": "text", "text": "fixed and passing"}),
            self.event("step_finish", {"sessionID": self.sid, "type": "step-finish",
                                        "reason": "stop", "tokens": {"input": 7, "output": 3}}),
        ]
        result, _, terminate = self.run_events(request_path, events)
        self.assertEqual(result["state"], "done")
        self.assertEqual(len(result["toolFailures"]), 1)
        self.assertEqual(result["toolFailures"][0]["error"], "Command exited with code 1")
        self.assertFalse(terminate.called)
        receipt = opencode.receipt(self.sid, "t1", self.slot)
        self.assertFalse(receipt["isError"])
        self.assertEqual(len(receipt["toolFailures"]), 1)

    def test_v205_permission_rejection_tool_event_is_blocked(self):
        request_path = self.request(turn="permission-rejected")
        event = self.event("tool_use", {"partID": "prt_question", "sessionID": self.sid,
                                        "type": "tool", "id": "call_q", "tool": "question",
                                        "state": {"status": "error", "input": {"questions": []},
                                                  "error": "Permission denied: question"}})
        result, _, terminate = self.run_events(request_path, [event], code=0)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("permission", result["error"])
        terminate.assert_called_once()

    def test_v205_interactive_question_cancellation_tool_event_is_blocked(self):
        request_path = self.request(turn="question-cancelled")
        event = self.event("tool_use", {"partID": "prt_question", "sessionID": self.sid,
                                        "type": "tool", "id": "call_q", "tool": "question",
                                        "state": {"status": "error", "input": {"questions": []},
                                                  "error": "The user dismissed this question"}})
        result, _, terminate = self.run_events(request_path, [event], code=0)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("cancelled", result["error"])
        terminate.assert_called_once()

    def test_length_stop_is_incomplete_and_not_verified_success(self):
        request_path = self.request(turn="length-stop")
        events = [
            self.event("step_start", {"sessionID": self.sid, "type": "step-start"}),
            self.event("text", {"sessionID": self.sid, "type": "text", "text": "truncated"}),
            self.event("step_finish", {"sessionID": self.sid, "type": "step-finish",
                                        "reason": "length"}),
        ]
        result, _, _ = self.run_events(request_path, events)
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["terminalReason"], "length")

    def test_cancel_queues_identity_bound_request_without_signaling_pid(self):
        turn = "cancel-turn"
        turn_path = self.data / self.sid / "turns" / turn
        turn_path.mkdir(parents=True)
        normalized = opencode.verify_slot(self.slot)
        (self.data / self.sid / "current.json").write_text(json.dumps({
            "turn": turn, "sessionId": self.sid, "cwd": normalized["cwd"], "model": opencode.MODEL}))
        (turn_path / "state.json").write_text(json.dumps({
            "sessionId": self.sid, "cwd": normalized["cwd"], "model": opencode.MODEL,
            "state": "working", "processIdentity": self.identity}))
        with patch.object(opencode, "identity_matches", return_value=True), \
                patch.object(opencode.os, "killpg") as killpg, \
                patch.object(opencode.os, "kill") as kill:
            result = opencode.cancel(self.slot)
        self.assertEqual(result, {"cancelRequested": self.identity["pid"], "turn": turn})
        killpg.assert_not_called()
        kill.assert_not_called()
        command = json.loads((turn_path / "cancel.json").read_text())
        self.assertEqual(command["processIdentity"], self.identity)

    def test_cancel_refuses_reused_or_unconfirmed_pid(self):
        turn = "stale-cancel"
        turn_path = self.data / self.sid / "turns" / turn
        turn_path.mkdir(parents=True)
        normalized = opencode.verify_slot(self.slot)
        (self.data / self.sid / "current.json").write_text(json.dumps({
            "turn": turn, "sessionId": self.sid, "cwd": normalized["cwd"], "model": opencode.MODEL}))
        (turn_path / "state.json").write_text(json.dumps({
            "sessionId": self.sid, "cwd": normalized["cwd"], "model": opencode.MODEL,
            "state": "working", "processIdentity": self.identity}))
        with patch.object(opencode, "identity_matches", return_value=False), \
                patch.object(opencode.os, "killpg") as killpg, \
                patch.object(opencode.os, "kill") as kill:
            with self.assertRaisesRegex(ValueError, "identity is not live"):
                opencode.cancel(self.slot)
        self.assertFalse((turn_path / "cancel.json").exists())
        killpg.assert_not_called()
        kill.assert_not_called()

    def test_cleanup_escalates_only_while_original_identity_is_confirmed(self):
        process = FakeProcess(events=())
        process.returncode = None
        with patch.object(opencode, "identity_matches", side_effect=[True, True]), \
                patch.object(opencode, "_group_exists", side_effect=[True, True, False]), \
                patch.object(opencode.os, "killpg") as killpg:
            result = opencode._terminate_owned_group(process, self.identity, grace=0, kill_grace=0)
        process.stdout.close()
        process.stdin.close()
        process.stdin_reader.close()
        self.assertEqual(result, {"confirmed": True, "uncertain": False})
        self.assertEqual([call.args[1] for call in killpg.call_args_list],
                         [opencode.signal.SIGTERM, opencode.signal.SIGKILL])

    def test_cleanup_never_kills_a_group_after_owned_process_exit(self):
        process = FakeProcess(events=())
        process.returncode = 0
        with patch.object(opencode, "identity_matches", return_value=False), \
                patch.object(opencode, "_group_exists", return_value=True), \
                patch.object(opencode.os, "killpg") as killpg:
            result = opencode._terminate_owned_group(process, self.identity, grace=0, kill_grace=0)
        process.stdout.close()
        process.stdin.close()
        process.stdin_reader.close()
        self.assertEqual(result, {"confirmed": False, "uncertain": True})
        killpg.assert_not_called()

    def test_current_process_identity_is_stable_and_detects_changed_start(self):
        identity = opencode.process_identity(os.getpid())
        self.assertIsNotNone(identity)
        self.assertTrue(opencode.identity_matches(identity))
        changed = dict(identity, start=[-1])
        self.assertFalse(opencode.identity_matches(changed))


if __name__ == "__main__":
    unittest.main()
