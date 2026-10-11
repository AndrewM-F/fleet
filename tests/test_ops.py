"""Operator actions, supervision, concurrency, timeouts and log upkeep."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import claude, dispatch, ops, paths, store  # noqa: E402


class Sandbox(unittest.TestCase):
    """Point every fleet path at a temporary root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.wt = root / "wt" / "repo" / "lane"
        self.wt.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.wt)], check=True)
        session = root / "pi-session.jsonl"
        session.write_text(json.dumps({"type": "session", "id": "pi-1", "cwd": str(self.wt)}) + "\n")
        self.cfg = {"worktreeRoot": str(root / "wt"),
                    "pi": [{"lane": "a", "sessionId": "pi-1", "cwd": str(self.wt),
                            "sessionFile": str(session), "sessionCwd": str(self.wt),
                            "provider": "x", "model": "m", "thinking": "high"}],
                    "claude": [{"sessionId": "c-1", "cwd": str(self.wt), "model": "m",
                                "effort": "high", "mode": "print", "resume": True,
                                "dispatchEnabled": True}]}
        (root / "config").mkdir()
        (root / "config" / "fleet.local.json").write_text(json.dumps(self.cfg))
        values = {"ROOT": root, "CONFIG": root / "config" / "fleet.local.json",
                  "DATA": root / "data", "LOGS": root / "logs", "DB": root / "data" / "f.sqlite",
                  "STATUS": root / "data" / "status.json", "CLAUDE": root / "data" / "claude",
                  "MANAGER_LOCK": root / "data" / "manager.lock"}
        for name, value in values.items():
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (root / "data").mkdir()
        wrapper = patch.object(claude, "wrapper_alive", return_value=True)
        wrapper.start()
        self.addCleanup(wrapper.stop)
        self.db = store.database()
        self.addCleanup(self.db.close)
        self.addCleanup(self.tmp.cleanup)

    def turn(self, state, pid=None):
        directory = paths.CLAUDE / "c-1" / "t1"
        directory.mkdir(parents=True, exist_ok=True)
        (directory.parent / "current.json").write_text(json.dumps({"turn": "t1", "startedAt": 0}))
        (directory / "state.json").write_text(json.dumps(
            {"sessionId": "c-1", "cwd": str(self.wt), "state": state, "hasSession": True,
             "wrapperPid": os.getpid(), "pid": pid, "progress": 3}))


class ResolveTests(Sandbox):
    def test_dead_wrapper_can_be_resolved_without_losing_native_identity(self):
        self.turn("working")
        slot = paths.slots()[1][0]
        with patch.object(claude, "wrapper_alive", return_value=False):
            self.assertEqual(claude.print_state(slot)["state"], "blocked")
            ops.resolve_worker(self.db, "c-1", "verified dead wrapper")
            state = claude.print_state(slot)
            self.assertEqual(state["state"], "idle")
            self.assertTrue(state["hasSession"])

    def test_events_acked_with_record(self):
        ids = [store.emit(self.db, "runner_error", error="x") for _ in range(2)]
        result = ops.resolve_events(self.db, ids + [9999], "migration noise")
        self.assertEqual(result["acked"], ids)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE acked=0").fetchone()[0], 0)

    def test_only_uncertain_tasks_resolve(self):
        self.db.execute("INSERT INTO tasks VALUES ('t','pi-1','/w','{}','uncertain',1)")
        self.db.execute("INSERT INTO tasks VALUES ('f','pi-1','/w','{}','finished',1)")
        self.assertEqual(ops.resolve_task(self.db, "t", "cancelled", "checked")["state"], "cancelled")
        with self.assertRaises(ValueError):
            ops.resolve_task(self.db, "f", "cancelled", "no")

    def test_failed_worker_becomes_dispatchable_keeping_session(self):
        self.turn("error")
        slot = paths.slots()[1][0]
        self.assertEqual(claude.print_state(slot)["state"], "error")
        ops.resolve_worker(self.db, "c-1", "turn was killed during migration")
        state = claude.print_state(slot)
        self.assertEqual(state["state"], "idle")
        self.assertTrue(state["hasSession"])  # next turn resumes, never re-creates

    def test_working_worker_is_not_resolvable(self):
        self.turn("working")
        with self.assertRaises(ValueError):
            ops.resolve_worker(self.db, "c-1", "x")


class CancelTests(Sandbox):
    def test_cancel_claude_turn_terminates_and_never_resends(self):
        self.turn("working", pid=424242)
        self.db.execute("INSERT INTO tasks VALUES ('t','c-1',?,'{}','running',1)", (str(self.wt),))
        self.db.commit()
        with patch.object(ops.os, "killpg") as kill:
            ops.cancel(self.db, "t", "wrong target")
        kill.assert_called_with(424242, ops.signal.SIGTERM)
        self.assertEqual(self.db.execute("SELECT state FROM tasks").fetchone()[0], "cancelled")

    def test_cancel_pi_sends_abort(self):
        self.db.execute("INSERT INTO tasks VALUES ('t','pi-1','/w','{}','sent',1)")
        with patch.object(ops.pi, "send", return_value={"forwarded": True}) as send:
            ops.cancel(self.db, "t", "x")
        self.assertEqual(send.call_args.args[1]["type"], "abort")


class ConcurrencyTests(Sandbox):
    def test_cap_limits_parallel_dispatch(self):
        slots, states = {}, {}
        for n in range(3):
            cwd = self.tmp.name + f"/w{n}"
            slots[f"s{n}"] = {"sessionId": f"s{n}", "provider": "pi"}
            states[f"s{n}"] = {"state": "idle", "cwd": cwd}
            self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                            (f"t{n}", f"s{n}", cwd, json.dumps({"message": "m"}), "queued", n))
        calls = []
        dispatch.dispatch(self.db, slots, states, lambda *a: calls.append(a), max_concurrent=2)
        self.assertEqual(len(calls), 2)


class SupervisorTests(Sandbox):
    def test_restarts_dead_lane_then_gives_up(self):
        history = {}
        with patch.object(ops, "start_manager", return_value=1), \
             patch.object(ops.pi, "running", return_value=False), \
             patch.object(ops.pi, "start", return_value={"ok": True}) as start:
            for n in range(5):
                ops.supervise_once(history, now=1000 + n)
        self.assertEqual(start.call_count, 3)
        kinds = [r[0] for r in self.db.execute("SELECT body FROM events WHERE kind='needs_attention'")]
        self.assertEqual(len(kinds), 2)  # manager + lane, once each

    def test_disabled_lane_and_supervisor_respected(self):
        (paths.DATA / "lanes" / "a").mkdir(parents=True)
        (paths.DATA / "lanes" / "a" / "disabled").write_text("x")
        with patch.object(ops, "start_manager", return_value=1), \
             patch.object(ops.pi, "running", return_value=False), \
             patch.object(ops.pi, "start") as start:
            ops.supervise_once({}, now=1)
        start.assert_not_called()
        (paths.DATA / "supervisor.disabled").write_text("x")
        with patch.object(ops, "start_manager") as manager:
            ops.supervise_once({}, now=2)
        manager.assert_not_called()

    def test_live_log_rotation_keeps_appending_writer(self):
        log = paths.LOGS / "lanes" / "a" / "events.jsonl"
        log.parent.mkdir(parents=True)
        writer = log.open("ab", buffering=0)
        writer.write(b"x" * 100)
        self.assertTrue(ops.copytruncate(log, 50, 2))
        writer.write(b"y\n")
        writer.close()
        self.assertEqual(log.read_bytes(), b"y\n")
        self.assertEqual(len(log.with_name("events.jsonl.1").read_bytes()), 100)


class RosterTests(Sandbox):
    def test_add_and_remove_claude(self):
        ops.add_claude("c-2", str(self.wt), "m", "high", new=True)
        self.assertEqual(len(paths.config()["claude"]), 2)
        self.assertEqual(paths.config()["claude"][-1]["permissionMode"], "bypassPermissions")
        with self.assertRaises(ValueError):
            ops.add_claude("c-2", str(self.wt), "m", "high", new=True)
        ops.remove("c-2")
        self.assertEqual(len(paths.config()["claude"]), 1)

    def test_rejects_cwd_outside_worktree_root(self):
        with self.assertRaises(ValueError):
            ops.add_claude("c-3", self.tmp.name, "m", "high", new=True)

    def test_remove_refuses_with_inflight_task(self):
        self.db.execute("INSERT INTO tasks VALUES ('t','c-1','/w','{}','running',1)")
        self.db.commit()
        with self.assertRaises(ValueError):
            ops.remove("c-1")


class TurnTimeoutTests(Sandbox):
    def test_hung_turn_is_terminated_and_marked_error(self):
        fake = Path(self.tmp.name) / "fake-claude"
        fake.write_text("#!/bin/sh\nsleep 30\n")
        fake.chmod(0o755)
        turn = paths.CLAUDE / "c-1" / "t9"
        turn.mkdir(parents=True)
        (turn / "request.json").write_text(json.dumps(
            {"sessionId": "c-1", "cwd": str(self.wt), "model": "m", "effort": "high",
             "message": "x", "resume": True, "executable": str(fake),
             "logDir": str(paths.LOGS / "t9"), "timeoutSeconds": 1}))
        started = time.time()
        claude.run_turn(turn / "request.json")
        state = json.loads((turn / "state.json").read_text())
        self.assertLess(time.time() - started, 10)
        self.assertEqual(state["state"], "error")
        self.assertIn("timeoutSeconds", state["error"])


if __name__ == "__main__":
    unittest.main()
