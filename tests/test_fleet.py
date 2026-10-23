"""Regression tests carried over from the pre-refactor fleet.py suite."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import claude, dispatch, manager, pi, store, util  # noqa: E402


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.sqlite"
        self.db = store.database(self.path)
        self.slot = {"sessionId": "s", "cwd": self.tmp.name, "provider": "pi"}
        self.slots = {"s": self.slot}
        self.states = {"s": {"state": "idle", "cwd": self.tmp.name, "progress": 1}}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def task(self, ident="one"):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (ident, "s", self.tmp.name, json.dumps({"message": "do work"}), "queued", 1))
        self.db.commit()

    def events(self, kind):
        return self.db.execute("SELECT count(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]

    def test_pause_and_once(self):
        self.task()
        calls = []
        def sender(*args):
            calls.append(args)
        dispatch.dispatch(self.db, self.slots, self.states, sender, paused=True)
        self.assertEqual(calls, [])
        dispatch.dispatch(self.db, self.slots, self.states, sender)
        dispatch.dispatch(self.db, self.slots, self.states, sender)
        self.assertEqual(len(calls), 1)

    def test_ambiguous_send_never_repeated_after_restart(self):
        self.task()

        def fail(*args):
            raise TimeoutError("uncertain")
        dispatch.dispatch(self.db, self.slots, self.states, fail)
        self.db.close()
        self.db = store.database(self.path)
        calls = []
        self.states["s"]["state"] = "idle"
        dispatch.dispatch(self.db, self.slots, self.states, lambda *args: calls.append(args))
        self.assertEqual(calls, [])
        self.assertEqual(self.events("task_uncertain"), 1)

    def test_writer_conflict(self):
        self.task()
        self.states["other"] = {"state": "working", "cwd": self.tmp.name}
        calls = []
        dispatch.dispatch(self.db, self.slots, self.states, lambda *a: calls.append(a))
        self.assertEqual(calls, [])

    def test_two_tasks_do_not_overlap(self):
        self.task("one")
        self.task("two")
        calls = []
        dispatch.dispatch(self.db, self.slots, self.states, lambda *a: calls.append(a))
        self.assertEqual(len(calls), 1)

    def test_completion_and_dedup(self):
        self.task()
        dispatch.dispatch(self.db, self.slots, self.states, lambda *a: None)
        manager.observe(self.db, "s", {"state": "streaming", "progress": 2}, now=10)
        manager.observe(self.db, "s", {"state": "idle", "progress": 3}, now=20)
        manager.observe(self.db, "s", {"state": "idle", "progress": 3}, now=30)
        self.assertEqual(self.events("task_consumed"), 1)
        self.assertEqual(self.events("review_ready"), 1)

    def test_stall_uses_progress(self):
        manager.observe(self.db, "s", {"state": "streaming", "progress": 1}, now=1)
        manager.observe(self.db, "s", {"state": "streaming", "progress": 2}, now=1000)
        manager.observe(self.db, "s", {"state": "streaming", "progress": 2}, now=1500)
        self.assertEqual(self.events("possibly_stalled"), 0)
        manager.observe(self.db, "s", {"state": "streaming", "progress": 2}, now=2300)
        manager.observe(self.db, "s", {"state": "streaming", "progress": 2}, now=2400)
        self.assertEqual(self.events("possibly_stalled"), 1)

    def test_unavailable_blocks_dispatch(self):
        self.task()
        self.states["other"] = {"state": "unavailable"}
        calls = []
        dispatch.dispatch(self.db, self.slots, self.states, lambda *a: calls.append(a))
        self.assertEqual(calls, [])

    def test_event_remains_until_acknowledged(self):
        ident = store.emit(self.db, "review_ready", sessionId="s")
        self.db.close()
        self.db = store.database(self.path)
        self.assertEqual(self.db.execute("SELECT id FROM events WHERE acked=0").fetchone()[0], ident)


class WorkspaceObservationTests(FleetTests):
    def test_known_other_workspace_unknown(self):
        self.task()
        self.states["other"] = {"state": "unknown", "cwd": self.tmp.name + "/other"}
        calls = []
        dispatch.dispatch(self.db, self.slots, self.states, lambda *a: calls.append(a))
        self.assertEqual(len(calls), 1)

    def test_same_workspace_unknown(self):
        self.task()
        self.states["other"] = {"state": "unknown", "cwd": self.tmp.name + "/."}
        calls = []
        dispatch.dispatch(self.db, self.slots, self.states, lambda *a: calls.append(a))
        self.assertEqual(calls, [])


class ClaudeResumeTests(unittest.TestCase):
    def test_closed_stdin_and_provider_pin(self):
        with patch.object(util.subprocess, "run") as run:
            run.return_value.stdout = "ok"
            self.assertEqual(util.command(["claude", "agents"]), "ok")
            self.assertEqual(run.call_args.kwargs["stdin"], util.subprocess.DEVNULL)
        slot = {"apiProvider": "anthropic", "model": "claude-haiku-5-5"}
        pi.verify_model(slot, {"model": {"provider": "anthropic", "id": "claude-haiku-5-5"}})
        with self.assertRaises(RuntimeError):
            pi.verify_model(slot, {"model": {"provider": "opencode-go", "id": "claude-haiku-5-5"}})

    def setUp(self):
        self.sid = "00000000-1111-4222-8333-444444444444"
        self.slot = {"sessionId": self.sid, "cwd": "/tmp/approved",
                     "provider": "claude", "model": "claude-haiku-5-5", "effort": "medium"}
        self.row = {"sessionId": self.sid, "id": "00000000",
                    "cwd": "/tmp/approved", "state": "done"}
        # This native transport fixture must not read an ignored source config.
        config = patch.object(claude.paths, "config", return_value={})
        config.start()
        self.addCleanup(config.stop)
        exe = patch.object(claude.paths, "executable", return_value="claude")
        exe.start()
        self.addCleanup(exe.stop)

    def test_stops_idle_process_before_native_resume(self):
        stopped = dict(self.row, state="stopped")
        working = dict(self.row, state="working")
        with patch.object(claude, "claude_rows", side_effect=[
                {self.sid: self.row}, {self.sid: stopped}, {self.sid: working}]), \
             patch.object(claude, "command", side_effect=["stopped 00000000", "backgrounded 00000000"]) as cmd, \
             patch.object(claude, "atomic"), patch.object(claude, "session_dir"):
            dispatch.send(self.slot, "approved task")
        self.assertEqual(cmd.call_args_list[0].args[0], ["claude", "stop", "00000000"])
        self.assertEqual(cmd.call_args_list[1].args[0][1:4], ["--resume", self.sid, "--bg"])

    def test_busy_session_is_never_stopped(self):
        with patch.object(claude, "claude_rows", return_value={
                self.sid: dict(self.row, state="working")}), patch.object(claude, "command") as cmd:
            with self.assertRaises(RuntimeError):
                dispatch.send(self.slot, "approved task")
            cmd.assert_not_called()

    def test_stopped_conversation_can_still_be_done(self):
        with patch.object(claude, "claude_rows", side_effect=[
                {self.sid: self.row}, {self.sid: self.row},
                {self.sid: dict(self.row, state="working")}]), \
             patch.object(claude, "command", side_effect=["stopped 00000000\n", "backgrounded 00000000"]), \
             patch.object(claude, "atomic"), patch.object(claude, "session_dir"):
            dispatch.send(self.slot, "approved task")

    def test_missing_stop_ack_never_resumes(self):
        with patch.object(claude, "claude_rows", return_value={self.sid: self.row}), \
             patch.object(claude, "command", return_value="No job matching 00000000") as cmd:
            with self.assertRaises(RuntimeError):
                dispatch.send(self.slot, "approved task")
            self.assertEqual(cmd.call_count, 1)

    def test_unexpected_copy_is_not_accepted(self):
        stopped = dict(self.row, state="stopped")
        with patch.object(claude, "claude_rows", side_effect=[
                {self.sid: stopped}, {self.sid: stopped, "new-id": dict(self.row, state="working")}]), \
             patch.object(claude, "command", return_value="backgrounded copy new-id"), \
             patch.object(claude, "atomic"), patch.object(claude, "session_dir"):
            with self.assertRaises(RuntimeError):
                dispatch.send(self.slot, "approved task")


if __name__ == "__main__":
    unittest.main()
