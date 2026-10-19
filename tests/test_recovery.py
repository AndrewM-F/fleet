"""Verified OpenCode recovery stays durable, single-use, and worktree-bound."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import control, dispatch, manager, opencode, paths, recovery, store  # noqa: E402


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "worktree"
        self.cwd.mkdir()
        self.native_cwd = self.root / "native-worker"
        self.native_cwd.mkdir()
        self.other_native_cwd = self.root / "native-other-worker"
        self.other_native_cwd.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "repair", str(self.cwd)], check=True)
        subprocess.run(["git", "-C", str(self.cwd), "config", "user.email", "fleet@test"],
                       check=True)
        subprocess.run(["git", "-C", str(self.cwd), "config", "user.name", "Fleet Test"],
                       check=True)
        (self.cwd / "partial.txt").write_text("preserve this partial work\n")
        subprocess.run(["git", "-C", str(self.cwd), "add", "partial.txt"], check=True)
        subprocess.run(["git", "-C", str(self.cwd), "commit", "-qm", "fixture"], check=True)
        (self.cwd / "untracked.txt").write_text("uncommitted owner state\n")
        self.sid = "ses_recovery_test"
        self.other_sid = "ses_recovery_other"
        self.turn = "turn_failed_1"
        self.failed_task = "failed-task"
        self.successor = "successor-task"
        self.slot = {"sessionId": self.sid, "cwd": str(self.native_cwd),
                     "sessionCwd": str(self.native_cwd),
                     "databasePath": str(self.root / "native.db"), "model": opencode.MODEL,
                     "provider": "opencode", "dispatchEnabled": True}
        with sqlite3.connect(self.slot["databasePath"]):
            pass
        self.other_slot = dict(self.slot, sessionId=self.other_sid,
                               cwd=str(self.other_native_cwd),
                               sessionCwd=str(self.other_native_cwd))
        for sid, _ in ((self.sid, self.failed_task),):
            self.prepare_failure(sid, self.failed_task, self.turn)
        self.db_path = self.root / "fleet.sqlite"
        self.db = store.database(self.db_path)
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (self.failed_task, self.sid, str(self.native_cwd),
                         json.dumps({"message": f"owner branch repair worktree {self.cwd.resolve()}",
                                     "sessionId": self.sid,
                                     "rootAuthorized": True}), "finished", 1))
        self.db.commit()
        self.data = self.root / "data"
        self.queue = self.data / "queue.json"
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.db.close)
        self.addCleanup(self.tmp.cleanup)
        patches = (
            ("OPENCODE", self.data / "opencode"), ("LOGS", self.root / "logs"),
            ("BIN", self.root / "bin" / "fleet"), ("QUEUE", self.queue),
            ("QUEUE_LOCK", self.data / "queue.lock"), ("PAUSE", self.data / "dispatch.paused"),
        )
        for name, value in patches:
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.slot_patch = patch.object(paths, "opencode_slots", return_value=[self.slot])
        self.slot_patch.start()
        self.addCleanup(self.slot_patch.stop)
        self.exec_patch = patch.object(paths, "executable", return_value="/usr/bin/opencode")
        self.exec_patch.start()
        self.addCleanup(self.exec_patch.stop)

    def prepare_failure(self, sid, task_id, turn, cwd=None):
        cwd = Path(cwd or self.native_cwd).resolve()
        session = self.root / "data" / "opencode" / sid
        turn_dir = session / "turns" / turn
        turn_dir.mkdir(parents=True, exist_ok=True)
        launch = {"turn": turn, "taskId": task_id, "sessionId": sid,
                  "cwd": str(cwd), "model": opencode.MODEL}
        (session / "current.json").write_text(json.dumps(launch))
        state = dict(launch, state="error", cleanup="confirmed",
                     error="OpenCode turn did not produce a verified native completion",
                     finishedAt=100)
        result = dict(taskId=task_id, sessionId=sid, cwd=str(cwd),
                      state="error", cleanup="confirmed", error=state["error"], toolFailures=[],
                      finishedAt=100)
        (turn_dir / "state.json").write_text(json.dumps(state))
        (turn_dir / "result.json").write_text(json.dumps(result))

    def inputs(self, **overrides):
        snap = recovery.worktree_snapshot(self.cwd)
        values = {"session_id": self.sid, "failed_task_id": self.failed_task,
                  "turn": self.turn, "recovery_id": "recovery-1",
                  "successor_task_id": self.successor, "ownership_task_id": self.failed_task,
                  "owned_worktree": str(self.cwd.resolve()), "branch": snap["branch"],
                  "head": snap["head"], "worktree_sha256": snap["worktreeSha256"],
                  "note": "confirmed failed completion and preserved partial checkout"}
        values.update(overrides)
        return values

    def authorize(self, **overrides):
        return recovery.authorize_opencode(self.db, **self.inputs(**overrides))

    def test_recovery_snapshot_cli_smoke(self):
        fleet_root = self.root / "isolated-fleet-root"
        env = dict(os.environ, FLEET_ROOT=str(fleet_root))
        env.pop("FLEET_CONFIG", None)
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "fleet"),
             "recovery-snapshot", "--cwd", str(self.cwd)],
            cwd=self.root, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), recovery.worktree_snapshot(self.cwd))
        self.assertFalse((fleet_root / "data" / "fleet.sqlite").exists())

    def test_owned_worktree_schema_migrates_in_place(self):
        legacy_path = self.root / "legacy-fleet.sqlite"
        old = sqlite3.connect(legacy_path)
        old.executescript("""
        CREATE TABLE events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL,
          created REAL NOT NULL, acked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE snapshots (sid TEXT PRIMARY KEY, body TEXT NOT NULL, changed REAL NOT NULL,
          observed REAL NOT NULL, stalled INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE tasks (id TEXT PRIMARY KEY, sid TEXT NOT NULL, cwd TEXT NOT NULL,
          body TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE recoveries (
          id TEXT PRIMARY KEY, failed_sid TEXT NOT NULL, failed_turn TEXT NOT NULL,
          failed_task_id TEXT NOT NULL, owner_sid TEXT NOT NULL, owner_task_id TEXT NOT NULL,
          cwd TEXT NOT NULL, branch TEXT NOT NULL, head TEXT NOT NULL,
          worktree_sha256 TEXT NOT NULL, note TEXT NOT NULL, status TEXT NOT NULL,
          created REAL NOT NULL, updated REAL NOT NULL,
          UNIQUE(failed_sid, failed_turn), UNIQUE(owner_task_id));
        INSERT INTO events(kind,body,created) VALUES ('preserved','{}',1);
        INSERT INTO tasks VALUES ('old-task','old-session','/old/cwd','{}','finished',1);
        INSERT INTO recoveries VALUES ('old-recovery','old-session','old-turn','old-task',
          'old-session','old-successor','/old/cwd','old-branch','old-head','old-digest',
          'old note','completed',1,1);
        INSERT INTO recoveries VALUES ('old-active-1','old-session-1','old-turn-1','old-task-1',
          'old-session-1','old-successor-1','/old/cwd-1','old-branch','old-head','old-digest',
          'old active note 1','authorized',2,2);
        INSERT INTO recoveries VALUES ('old-active-2','old-session-2','old-turn-2','old-task-2',
          'old-session-2','old-successor-2','/old/cwd-2','old-branch','old-head','old-digest',
          'old active note 2','claimed',3,3);
        """)
        old.close()

        migrated = store.database(legacy_path)
        self.addCleanup(migrated.close)
        row = migrated.execute("SELECT status,owner_task_id,owned_worktree,ownership_task_id "
                               "FROM recoveries WHERE id='old-recovery'").fetchone()
        self.assertEqual(row, ("completed", "old-successor", "", ""))
        self.assertEqual(migrated.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        self.assertEqual(migrated.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertEqual(migrated.execute("SELECT count(*) FROM recoveries").fetchone()[0], 3)
        self.assertEqual(migrated.execute(
            "SELECT count(*) FROM recoveries WHERE status IN ('authorized','claimed')"
        ).fetchone()[0], 2)

    def add_second_failed_task(self):
        second_turn = "turn_failed_2"
        self.prepare_failure(self.other_sid, "failed-task-2", second_turn, self.other_native_cwd)
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        ("failed-task-2", self.other_sid, str(self.other_native_cwd),
                         json.dumps({"message": f"branch repair worktree {self.cwd.resolve()}"}),
                         "finished", 2))
        self.db.commit()
        self.slot_patch.stop()
        self.slot_patch = patch.object(paths, "opencode_slots",
                                       return_value=[self.slot, self.other_slot])
        self.slot_patch.start()
        self.addCleanup(self.slot_patch.stop)

    def test_verified_failure_creates_immutable_idempotent_record(self):
        self.assertNotEqual(str(self.native_cwd), str(self.cwd))
        self.assertEqual(self.authorize(), {"recoveryId": "recovery-1", "status": "authorized",
                                            "alreadyRecorded": False})
        row = self.db.execute("SELECT status,failed_task_id,owner_task_id,cwd,owned_worktree,branch,head,"
                              "worktree_sha256 "
                              "FROM recoveries WHERE id='recovery-1'").fetchone()
        self.assertEqual(row, ("authorized", self.failed_task, self.successor,
                               str(self.native_cwd.resolve()), str(self.cwd.resolve()),
                               recovery.worktree_snapshot(self.cwd)["branch"],
                               recovery.worktree_snapshot(self.cwd)["head"],
                               recovery.worktree_snapshot(self.cwd)["worktreeSha256"]))
        self.assertEqual(self.authorize()["alreadyRecorded"], True)
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.failed_task,)).fetchone()[0], "finished")
        self.assertEqual(json.loads((paths.OPENCODE / self.sid / "turns" / self.turn /
                                     "state.json").read_text())["state"], "error")
        with self.assertRaisesRegex(ValueError, "different content"):
            self.authorize(note="different operator claim")

    def test_owned_worktree_must_be_in_finished_owner_task(self):
        other = self.root / "unowned-worktree"
        other.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "other", str(other)], check=True)
        snap = recovery.worktree_snapshot(self.cwd)
        with self.assertRaisesRegex(ValueError, "not recorded by this worker's owner task"):
            self.authorize(owned_worktree=str(other.resolve()), branch=snap["branch"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_active_lock_prevents_authorization(self):
        lock = opencode._session_lock(paths.OPENCODE / self.sid)
        try:
            with self.assertRaisesRegex(RuntimeError, "already has a Fleet turn"):
                self.authorize()
        finally:
            lock.close()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_incomplete_cleanup_and_rate_limit_are_not_recoverable(self):
        state_path = paths.OPENCODE / self.sid / "turns" / self.turn / "state.json"
        result_path = state_path.with_name("result.json")
        result = json.loads(result_path.read_text())
        state = json.loads(state_path.read_text())
        state["cleanup"] = result["cleanup"] = "uncertain"
        state_path.write_text(json.dumps(state))
        result_path.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "cleanup is not verified"):
            self.authorize()
        state["cleanup"] = result["cleanup"] = "confirmed"
        result["error"] = "HTTP 429: rate limit exceeded"
        state_path.write_text(json.dumps(state))
        result_path.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "Rate-limit/quota"):
            self.authorize()
        result["error"] = "HTTP 403: provider access forbidden"
        result_path.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "Rate-limit/quota"):
            self.authorize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_branch_head_and_partial_worktree_must_match_snapshot(self):
        snap = recovery.worktree_snapshot(self.cwd)
        (self.cwd / "untracked.txt").write_text("changed after inspection\n")
        with self.assertRaisesRegex(ValueError, "fingerprint changed"):
            self.authorize(branch=snap["branch"], head=snap["head"],
                           worktree_sha256=snap["worktreeSha256"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_pause_and_model_dispatch_hold_are_not_cleared(self):
        self.authorize()
        paths.PAUSE.write_text("preserve hold\n")
        task = {"id": self.successor, "previousTaskId": self.failed_task,
                "recoveryId": "recovery-1", "rootAuthorized": True, "message": "continue"}
        with patch.object(paths, "all_slots", return_value=[self.slot]):
            control.enqueue(self.db, [task])
        manager.import_queue(self.db, {self.sid: self.slot}, self.queue)
        calls = []
        dispatch.dispatch(self.db, {self.sid: self.slot},
                          {self.sid: {"state": "error", "cwd": str(self.native_cwd)}},
                          lambda *args, **kwargs: calls.append((args, kwargs)), paused=True)
        self.assertEqual(calls, [])
        self.assertTrue(paths.PAUSE.exists())
        self.assertEqual(self.db.execute("SELECT status FROM recoveries").fetchone()[0],
                         "authorized")
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "queued")

    def test_successor_must_be_linked_to_exact_recovery_record(self):
        self.authorize()
        bad = {"id": self.successor, "previousTaskId": self.failed_task,
               "recoveryId": "other-recovery", "rootAuthorized": True, "message": "continue"}
        with patch.object(paths, "all_slots", return_value=[self.slot]):
            with self.assertRaisesRegex(ValueError, "authorized durable handoff"):
                control.enqueue(self.db, [bad])
        self.assertFalse(self.queue.exists())

    def test_disabled_worker_cannot_be_enabled_by_recovery(self):
        disabled = dict(self.slot, dispatchEnabled=False)
        with patch.object(paths, "opencode_slots", return_value=[disabled]):
            with self.assertRaisesRegex(ValueError, "dispatch is disabled"):
                self.authorize()
        self.assertFalse(disabled["dispatchEnabled"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_owner_exclusivity_blocks_a_second_session_for_same_worktree(self):
        self.authorize()
        self.add_second_failed_task()
        snap = recovery.worktree_snapshot(self.cwd)
        with self.assertRaisesRegex(ValueError, "active recovery already owns"):
            recovery.authorize_opencode(self.db, session_id=self.other_sid,
                failed_task_id="failed-task-2", turn="turn_failed_2", recovery_id="recovery-2",
                successor_task_id="successor-2", ownership_task_id="failed-task-2",
                owned_worktree=str(self.cwd.resolve()), branch=snap["branch"], head=snap["head"],
                worktree_sha256=snap["worktreeSha256"], note="second owner")
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 1)

    def test_active_recovery_reserves_owned_worktree_across_sessions(self):
        self.authorize()
        self.add_second_failed_task()
        task = {"id": "other-successor", "sessionId": self.other_sid,
                "rootAuthorized": True,
                "message": f"Continue the task in {self.cwd.resolve()}"}
        with patch.object(paths, "all_slots", return_value=[self.slot, self.other_slot]):
            with self.assertRaisesRegex(ValueError, "conflicts with active recovery"):
                control.enqueue(self.db, [task])
        self.assertFalse(self.queue.exists())

    def test_successor_claim_is_deduplicated_across_manager_restart(self):
        self.authorize()
        task = {"id": self.successor, "previousTaskId": self.failed_task,
                "recoveryId": "recovery-1", "rootAuthorized": True, "message": "continue"}
        with patch.object(paths, "all_slots", return_value=[self.slot]):
            control.enqueue(self.db, [task])
        manager.import_queue(self.db, {self.sid: self.slot}, self.queue)
        sent = []

        def interrupt_after_claim(*args, **kwargs):
            sent.append((args, kwargs))
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            dispatch.dispatch(self.db, {self.sid: self.slot},
                {self.sid: {"state": "error", "cwd": str(self.native_cwd)}},
                interrupt_after_claim)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1]["recovery"]["status"], "claimed")
        self.db.close()
        self.db = store.database(self.db_path)
        manager.recover(self.db)
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.failed_task,)).fetchone()[0], "finished")
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "uncertain")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries").fetchone()[0], "claimed")
        with patch.object(paths, "all_slots", return_value=[self.slot]):
            with self.assertRaisesRegex(ValueError, "conflicts with active recovery"):
                control.enqueue(self.db, [{"id": self.failed_task, "sessionId": self.sid,
                    "rootAuthorized": True, "message": "original task"}])
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.failed_task,)).fetchone()[0], "finished")
        dispatch.dispatch(self.db, {self.sid: self.slot},
            {self.sid: {"state": "error", "cwd": str(self.native_cwd)}},
            lambda *args, **kwargs: sent.append((args, kwargs)))
        self.assertEqual(len(sent), 1)

    def test_worktree_change_after_authorization_blocks_native_send(self):
        record_id = self.authorize()["recoveryId"]
        record = recovery.recovery_for_task(self.db, record_id, self.successor,
                                             self.sid, self.failed_task)
        self.db.execute("UPDATE recoveries SET status='claimed' WHERE id=?", (record_id,))
        record["status"] = "claimed"
        (self.cwd / "untracked.txt").write_text("changed after recovery record\n")
        with self.assertRaisesRegex(RuntimeError, "worktree changed after authorization"):
            opencode.send(self.slot, "continue", self.successor, record)
        launch = json.loads((paths.OPENCODE / self.sid / "current.json").read_text())
        self.assertEqual(launch["turn"], self.turn)

    def test_continue_prompt_binds_owned_checkout_without_rehoming_session(self):
        record_id = self.authorize()["recoveryId"]
        record = recovery.claim_recovery(self.db, record_id, self.successor,
                                         self.sid, self.failed_task)
        self.db.commit()
        native = {"sessionCwd": str(self.native_cwd.resolve()),
                  "databasePath": self.slot["databasePath"]}
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            if args[0][0] == "git":
                return real_popen(*args, **kwargs)
            return Mock(pid=12345)

        with patch.object(opencode, "verify_native_session", return_value=native), \
                patch.object(opencode.subprocess, "Popen", side_effect=launch) as popen:
            opencode.send(self.slot, "continue", self.successor, record)
        launch = json.loads((paths.OPENCODE / self.sid / "current.json").read_text())
        request = json.loads((paths.OPENCODE / self.sid / "turns" / launch["turn"] /
                              "request.json").read_text())
        self.assertIn(str(self.cwd.resolve()), request["message"])
        self.assertIn('"branch": "repair"', request["message"])
        self.assertTrue(request["message"].endswith("continue"))
        self.assertEqual(request["ownedWorktree"], str(self.cwd.resolve()))
        self.assertEqual(request["branch"], "repair")
        self.assertEqual(request["cwd"], str(self.native_cwd.resolve()))
        self.assertEqual(request["sessionCwd"], str(self.native_cwd.resolve()))
        self.assertEqual(popen.call_args.kwargs["cwd"], str(self.native_cwd.resolve()))
        self.assertEqual(launch["cwd"], str(self.native_cwd.resolve()))

    def test_checkout_errors_do_not_block_later_queued_tasks(self):
        # Exercise real path/Git failures plus a read failure during fingerprinting.
        for failure in ("missing", "damaged-git", "unreadable-file"):
            with self.subTest(failure=failure):
                self._assert_checkout_error_is_contained(failure)

    def _assert_checkout_error_is_contained(self, failure):
        # Each case starts from a fresh recovery/queue, using the same fixture checkout.
        self.db.execute("DELETE FROM recoveries")
        self.db.execute("DELETE FROM tasks WHERE id<>?", (self.failed_task,))
        self.db.execute("DELETE FROM events")
        self.db.commit()
        self.queue.unlink(missing_ok=True)
        self.authorize()
        task = {"id": self.successor, "previousTaskId": self.failed_task,
                "recoveryId": "recovery-1", "rootAuthorized": True, "message": "continue"}
        with patch.object(paths, "all_slots", return_value=[self.slot]):
            control.enqueue(self.db, [task])
        manager.import_queue(self.db, {self.sid: self.slot}, self.queue)
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        ("later-task", self.other_sid, str(self.other_native_cwd),
                         json.dumps({"message": "independent task"}), "queued", 1e20))
        self.db.commit()
        moved = None
        read_patch = None
        try:
            if failure == "missing":
                moved = self.cwd.with_name("moved-worktree")
                self.cwd.rename(moved)
            elif failure == "damaged-git":
                moved = self.cwd / "saved-git"
                (self.cwd / ".git").rename(moved)
            else:
                read_patch = patch.object(Path, "open", side_effect=PermissionError("unreadable"))
                read_patch.start()
            sent = []
            slots = {self.sid: self.slot, self.other_sid: self.other_slot}
            states = {self.sid: {"state": "error", "cwd": str(self.native_cwd)},
                      self.other_sid: {"state": "idle", "cwd": str(self.other_native_cwd)}}
            dispatch.dispatch(self.db, slots, states,
                              lambda *args, **kwargs: sent.append(kwargs["task_id"]))
            self.assertEqual(sent, ["later-task"])
            self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                             (self.successor,)).fetchone()[0], "queued")
            self.assertEqual(self.db.execute("SELECT status FROM recoveries").fetchone()[0],
                             "authorized")
            # Re-observation reports once and never claims/sends the invalid successor.
            dispatch.dispatch(self.db, slots, states, lambda *args, **kwargs: self.fail("resent"))
            events = self.db.execute("SELECT body FROM events WHERE kind='needs_attention'").fetchall()
            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0][0])["taskId"], self.successor)
        finally:
            if read_patch:
                read_patch.stop()
            if moved:
                moved.rename(self.cwd if failure == "missing" else self.cwd / ".git")

    def test_live_recorded_native_process_blocks_recovery(self):
        result_path = paths.OPENCODE / self.sid / "turns" / self.turn / "result.json"
        result = json.loads(result_path.read_text())
        result["managedProcessIdentity"] = {"pid": 123, "pgid": 123}
        result_path.write_text(json.dumps(result))
        with patch.object(opencode, "identity_matches", return_value=True):
            with self.assertRaisesRegex(ValueError, "process group is still live"):
                self.authorize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_completed_successor_releases_active_owner_reservation(self):
        self.authorize()
        task = {"id": self.successor, "previousTaskId": self.failed_task,
                "recoveryId": "recovery-1", "rootAuthorized": True, "message": "continue"}
        with patch.object(paths, "all_slots", return_value=[self.slot]):
            control.enqueue(self.db, [task])
        manager.import_queue(self.db, {self.sid: self.slot}, self.queue)
        dispatch.dispatch(self.db, {self.sid: self.slot},
                          {self.sid: {"state": "error", "cwd": str(self.native_cwd)}},
                          lambda *args, **kwargs: None)
        manager.observe(self.db, self.sid, {"state": "working", "cwd": str(self.native_cwd),
                                            "progress": 1}, now=10)
        manager.observe(self.db, self.sid, {"state": "done", "cwd": str(self.native_cwd),
                                            "progress": 2}, now=20)
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "finished")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "completed")


if __name__ == "__main__":
    unittest.main()
