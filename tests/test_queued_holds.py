"""Queued-task holds are durable, idempotent, and bounded to one task.

No provider or model is called: dispatch senders are recording fakes and the only
subprocess work is disposable Git fixture setup for the recovery-handoff case.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import control, dispatch, holds, manager, opencode, paths, recovery, store  # noqa: E402

NON_QUEUED = ("dispatching", "sent", "running", "uncertain", "finished", "cancelled")


class QueuedHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "fleet.sqlite"
        self.db = store.database(self.db_path)
        self.addCleanup(self.db.close)
        self.addCleanup(self.tmp.cleanup)
        self.cwd = str(self.root)
        self.slot = {"sessionId": "worker", "cwd": self.cwd, "provider": "opencode",
                     "dispatchEnabled": True}
        self.slots = {"worker": self.slot}
        self.states = {"worker": {"state": "idle", "cwd": self.cwd}}
        self.pause = self.root / "data" / "dispatch.paused"
        self.pause.parent.mkdir(parents=True, exist_ok=True)
        for name, value in (("PAUSE", self.pause), ("QUEUE", self.root / "data" / "queue.json")):
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def add_task(self, task_id, state="queued", sid="worker"):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (task_id, sid, self.cwd,
                         json.dumps({"id": task_id, "sessionId": sid,
                                     "rootAuthorized": True, "message": "work " + task_id}),
                         state, 1.0))
        self.db.commit()

    def task_row(self, task_id):
        return self.db.execute("SELECT id,sid,cwd,body,state FROM tasks WHERE id=?",
                               (task_id,)).fetchone()

    def run_dispatch(self, calls, **kwargs):
        dispatch.dispatch(self.db, self.slots, self.states,
                          lambda *args, **rest: calls.append((args, rest)), **kwargs)

    # --- idempotence and task identity -------------------------------------
    def test_hold_is_idempotent_and_preserves_task_identity(self):
        self.add_task("t1")
        before = self.task_row("t1")
        first = holds.hold(self.db, "t1", "operator pause")
        self.assertEqual(first, {"taskId": "t1", "held": True, "alreadyHeld": False,
                                 "reason": "operator pause"})
        repeat = holds.hold(self.db, "t1", "a different reason is ignored on repeat")
        self.assertEqual(repeat["alreadyHeld"], True)
        self.assertEqual(repeat["reason"], "operator pause")
        self.assertEqual(self.task_row("t1"), before)  # same row, same ID, same body
        self.assertEqual(self.db.execute("SELECT count(*) FROM task_holds").fetchone()[0], 1)
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM events WHERE kind='task_held'").fetchone()[0], 1)
        self.assertTrue(holds.is_held(self.db, "t1"))
        self.assertEqual(holds.current(self.db, "t1")["reason"], "operator pause")

    def test_release_is_idempotent(self):
        self.add_task("t1")
        holds.hold(self.db, "t1", "pause")
        first = holds.release(self.db, "t1", "resume")
        self.assertEqual(first, {"taskId": "t1", "held": False, "alreadyReleased": False})
        repeat = holds.release(self.db, "t1", "resume again")
        self.assertEqual(repeat["alreadyReleased"], True)
        self.assertFalse(holds.is_held(self.db, "t1"))
        self.assertEqual(self.db.execute("SELECT state FROM tasks").fetchone()[0], "queued")
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM events WHERE kind='task_released'").fetchone()[0], 1)

    def test_release_of_never_held_task_is_a_no_op(self):
        self.add_task("t1")
        self.assertEqual(holds.release(self.db, "t1", "nothing to clear"),
                         {"taskId": "t1", "held": False, "alreadyReleased": True})
        self.assertEqual(self.db.execute("SELECT count(*) FROM task_holds").fetchone()[0], 0)

    # --- running / terminal rejection --------------------------------------
    def test_hold_rejects_started_and_terminal_tasks(self):
        for state in NON_QUEUED:
            with self.subTest(state=state):
                self.add_task("h-" + state, state=state)
                with self.assertRaisesRegex(ValueError, "unstarted queued"):
                    holds.hold(self.db, "h-" + state, "nope")
                self.assertFalse(holds.is_held(self.db, "h-" + state))

    def test_release_rejects_started_and_terminal_tasks(self):
        for state in NON_QUEUED:
            with self.subTest(state=state):
                self.add_task("r-" + state, state=state)
                with self.assertRaisesRegex(ValueError, "unstarted queued"):
                    holds.release(self.db, "r-" + state, "nope")

    def test_hold_and_release_require_a_task_and_a_reason(self):
        with self.assertRaisesRegex(ValueError, "Task not found"):
            holds.hold(self.db, "missing", "nope")
        self.add_task("t1")
        with self.assertRaisesRegex(ValueError, "non-empty"):
            holds.hold(self.db, "t1", "   ")
        with self.assertRaisesRegex(ValueError, "non-empty"):
            holds.release(self.db, "t1", "")

    # --- durability and dispatch -------------------------------------------
    def test_hold_survives_restart_and_blocks_dispatch(self):
        self.add_task("t1")
        holds.hold(self.db, "t1", "durable pause")
        self.db.close()
        self.db = store.database(self.db_path)  # manager restart
        manager.recover(self.db)
        calls = []
        self.run_dispatch(calls)
        self.assertEqual(calls, [])
        self.assertTrue(holds.is_held(self.db, "t1"))
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id='t1'")
                         .fetchone()[0], "queued")
        calls.clear()
        holds.release(self.db, "t1", "resume")
        self.run_dispatch(calls)
        self.assertEqual(len(calls), 1)

    def test_held_task_does_not_consume_or_block_its_queued_sibling(self):
        self.add_task("held")
        self.add_task("sibling")
        holds.hold(self.db, "held", "keep waiting")
        calls = []
        self.run_dispatch(calls)
        self.assertEqual(len(calls), 1)                 # only the sibling dispatched
        self.assertIn("sibling", calls[0][0][1])        # and it carries the sibling message
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id='held'")
                         .fetchone()[0], "queued")
        self.assertTrue(holds.is_held(self.db, "held"))
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id='sibling'")
                         .fetchone()[0], "sent")

    def test_release_clears_only_the_task_level_hold(self):
        self.add_task("t1")
        holds.hold(self.db, "t1", "pause")
        self.pause.write_text("paused\n")               # global pause gate
        self.slot["dispatchEnabled"] = False            # per-worker dispatch hold
        self.db.execute("INSERT INTO recoveries (id,failed_sid,failed_turn,failed_task_id,"
                        "owner_sid,owner_task_id,ownership_task_id,cwd,owned_worktree,branch,"
                        "head,worktree_sha256,note,status,created,updated) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("resv", "worker", "turn", "failed", "worker", "owner-task", "own-task",
                         self.cwd, self.cwd, "branch", "head", "digest",
                         "note", "authorized", 1.0, 1.0))
        self.db.commit()
        reserved = self.db.execute("SELECT * FROM recoveries WHERE id='resv'").fetchone()
        self.assertEqual(holds.release(self.db, "t1", "resume"),
                         {"taskId": "t1", "held": False, "alreadyReleased": False})
        self.assertFalse(holds.is_held(self.db, "t1"))
        self.assertTrue(self.pause.exists())            # pause survives
        self.assertFalse(self.slot["dispatchEnabled"])  # per-worker hold survives
        self.assertEqual(self.db.execute("SELECT * FROM recoveries WHERE id='resv'")
                         .fetchone(), reserved)         # reservation survives
        calls = []
        self.run_dispatch(calls, paused=True)           # global pause still gates dispatch
        self.run_dispatch(calls)                        # dispatchEnabled still gates dispatch
        self.assertEqual(calls, [])


class HeldRecoverySuccessorTests(unittest.TestCase):
    """A held recovery successor is never claimed; release keeps the reservation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "worktree"    # separately verified owned checkout
        self.cwd.mkdir()
        self.native = self.root / "native"   # fixed native session cwd
        self.native.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "repair", str(self.cwd)], check=True)
        subprocess.run(["git", "-C", str(self.cwd), "config", "user.email", "fleet@test"],
                       check=True)
        subprocess.run(["git", "-C", str(self.cwd), "config", "user.name", "Fleet Test"],
                       check=True)
        (self.cwd / "partial.txt").write_text("preserve this partial work\n")
        subprocess.run(["git", "-C", str(self.cwd), "add", "partial.txt"], check=True)
        subprocess.run(["git", "-C", str(self.cwd), "commit", "-qm", "fixture"], check=True)
        (self.cwd / "untracked.txt").write_text("uncommitted owner state\n")
        self.sid = "ses_hold_recovery"
        self.turn = "turn_failed_hold"
        self.failed = "failed-hold-task"
        self.successor = "successor-hold-task"
        self.slot = {"sessionId": self.sid, "cwd": str(self.native),
                     "sessionCwd": str(self.native),
                     "databasePath": str(self.root / "native.db"), "model": opencode.MODEL,
                     "provider": "opencode", "dispatchEnabled": True}
        with sqlite3.connect(self.slot["databasePath"]):
            pass
        self.db_path = self.root / "fleet.sqlite"
        self.db = store.database(self.db_path)
        self.addCleanup(self.db.close)
        self.addCleanup(self.tmp.cleanup)
        self.data = self.root / "data"
        self.queue = self.data / "queue.json"
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        for name, value in (("OPENCODE", self.data / "opencode"), ("LOGS", self.root / "logs"),
                            ("QUEUE", self.queue), ("QUEUE_LOCK", self.data / "queue.lock")):
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        slots = patch.object(paths, "opencode_slots", return_value=[self.slot])
        slots.start()
        self.addCleanup(slots.stop)
        executable = patch.object(paths, "executable", return_value="/usr/bin/opencode")
        executable.start()
        self.addCleanup(executable.stop)
        self.prepare_failure()
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (self.failed, self.sid, str(self.native),
                         json.dumps({"message": f"owner repair worktree {self.cwd.resolve()}",
                                     "sessionId": self.sid, "rootAuthorized": True}),
                         "finished", 1.0))
        self.db.commit()

    def prepare_failure(self):
        session = self.data / "opencode" / self.sid
        turn_dir = session / "turns" / self.turn
        turn_dir.mkdir(parents=True, exist_ok=True)
        launch = {"turn": self.turn, "taskId": self.failed, "sessionId": self.sid,
                  "cwd": str(self.native.resolve()), "model": opencode.MODEL}
        (session / "current.json").write_text(json.dumps(launch))
        state = dict(launch, state="error", cleanup="confirmed", error="failed", finishedAt=100)
        result = dict(taskId=self.failed, sessionId=self.sid, cwd=str(self.native.resolve()),
                      state="error", cleanup="confirmed", error="failed", toolFailures=[],
                      finishedAt=100)
        (turn_dir / "state.json").write_text(json.dumps(state))
        (turn_dir / "result.json").write_text(json.dumps(result))

    def authorize(self):
        snap = recovery.worktree_snapshot(self.cwd)
        return recovery.authorize_opencode(
            self.db, session_id=self.sid, failed_task_id=self.failed, turn=self.turn,
            recovery_id="recovery-hold", successor_task_id=self.successor,
            ownership_task_id=self.failed, owned_worktree=str(self.cwd.resolve()),
            branch=snap["branch"], head=snap["head"], worktree_sha256=snap["worktreeSha256"],
            note="verified cleanup and preserved partial work")

    def test_held_successor_is_excluded_and_release_keeps_the_reservation(self):
        self.authorize()
        task = {"id": self.successor, "previousTaskId": self.failed,
                "recoveryId": "recovery-hold", "rootAuthorized": True, "message": "continue"}
        with patch.object(paths, "all_slots", return_value=[self.slot]):
            control.enqueue(self.db, [task])
        manager.import_queue(self.db, {self.sid: self.slot}, self.queue)
        holds.hold(self.db, self.successor, "operator hold on the successor")
        sent = []
        states = {self.sid: {"state": "error", "cwd": str(self.native)}}
        dispatch.dispatch(self.db, {self.sid: self.slot}, states,
                          lambda *args, **rest: sent.append((args, rest)))
        self.assertEqual(sent, [])                       # held task never claimed or sent
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-hold'")
                         .fetchone()[0], "authorized")   # durable reservation intact
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "queued")
        self.assertEqual(holds.release(self.db, self.successor, "resume"),
                         {"taskId": self.successor, "held": False, "alreadyReleased": False})
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-hold'")
                         .fetchone()[0], "authorized")   # release cleared only the hold
        dispatch.dispatch(self.db, {self.sid: self.slot}, states,
                          lambda *args, **rest: sent.append((args, rest)))
        self.assertEqual(len(sent), 1)                   # reservation still governs dispatch
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-hold'")
                         .fetchone()[0], "claimed")


class HoldCliTests(unittest.TestCase):
    def test_hold_and_release_cli_are_supported_and_report_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            config = root / "fleet.json"
            config.write_text("{}")
            env = dict(os.environ, FLEET_ROOT=str(root), FLEET_CONFIG=str(config))
            db = store.database(root / "data" / "fleet.sqlite")
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                       ("cli-task", "worker", str(root),
                        json.dumps({"id": "cli-task", "sessionId": "worker",
                                    "rootAuthorized": True, "message": "work"}),
                        "queued", 1.0))
            db.commit()
            db.close()
            fleet = str(Path(__file__).resolve().parents[1] / "bin" / "fleet")
            held = subprocess.run([sys.executable, fleet, "hold", "cli-task",
                                   "--note", "cli pause"],
                                  env=env, capture_output=True, text=True)
            self.assertEqual(held.returncode, 0, held.stderr)
            self.assertEqual(json.loads(held.stdout)["held"], True)
            released = subprocess.run([sys.executable, fleet, "release", "cli-task",
                                       "--note", "cli resume"],
                                      env=env, capture_output=True, text=True)
            self.assertEqual(released.returncode, 0, released.stderr)
            self.assertEqual(json.loads(released.stdout)["held"], False)
            rejected = subprocess.run([sys.executable, fleet, "hold", "missing",
                                       "--note", "x"],
                                      env=env, capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Task not found", json.loads(rejected.stdout)["message"])


if __name__ == "__main__":
    unittest.main()
