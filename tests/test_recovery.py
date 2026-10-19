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

from fleet import control, dispatch, manager, opencode, ops, paths, recovery, store  # noqa: E402


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

    def test_legacy_recovery_migration_rolls_back_as_one_transaction(self):
        legacy_path = self.root / "interrupted-legacy-fleet.sqlite"
        old = sqlite3.connect(legacy_path)
        old.executescript("""
        CREATE TABLE recoveries (
          id TEXT PRIMARY KEY, failed_sid TEXT NOT NULL, failed_turn TEXT NOT NULL,
          failed_task_id TEXT NOT NULL, owner_sid TEXT NOT NULL, owner_task_id TEXT NOT NULL,
          cwd TEXT NOT NULL, branch TEXT NOT NULL, head TEXT NOT NULL,
          worktree_sha256 TEXT NOT NULL, note TEXT NOT NULL, status TEXT NOT NULL,
          created REAL NOT NULL, updated REAL NOT NULL,
          UNIQUE(failed_sid, failed_turn), UNIQUE(owner_task_id));
        CREATE INDEX preserved_legacy_lookup ON recoveries(status, owner_sid);
        INSERT INTO recoveries VALUES ('r1','s1','t1','f1','s1','o1','/w1','b1','h1','d1','n1','authorized',1,1);
        INSERT INTO recoveries VALUES ('r2','s2','t2','f2','s2','o2','/w2','b2','h2','d2','n2','claimed',2,2);
        INSERT INTO recoveries VALUES ('r3','s3','t3','f3','s3','o3','/w3','b3','h3','d3','n3','completed',3,3);
        """)
        old.close()

        real = sqlite3.connect(legacy_path)

        class InterruptCopy:
            def execute(self, sql, *args):
                if sql.startswith("INSERT INTO recoveries ("):
                    raise RuntimeError("simulated process interruption during row copy")
                return real.execute(sql, *args)

            def executescript(self, sql):
                return real.executescript(sql)

            def commit(self):
                return real.commit()

            def rollback(self):
                return real.rollback()

        with self.assertRaisesRegex(RuntimeError, "simulated process interruption"):
            store._retire_legacy_recoveries(InterruptCopy())
        real.close()

        # Reopening after an interrupted migration must see the complete old schema,
        # rows, and indexes, not a new empty table shadowing recoveries_legacy.
        check = sqlite3.connect(legacy_path)
        self.assertIsNotNone(check.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recoveries'"
        ).fetchone())
        self.assertIsNone(check.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recoveries_legacy'"
        ).fetchone())
        self.assertEqual(check.execute("SELECT id,status FROM recoveries ORDER BY id").fetchall(),
                         [("r1", "authorized"), ("r2", "claimed"), ("r3", "completed")])
        self.assertIsNotNone(check.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='preserved_legacy_lookup'"
        ).fetchone())
        check.close()

        migrated = store.database(legacy_path)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.execute(
            "SELECT id,failed_sid,failed_turn,failed_task_id,owner_sid,owner_task_id,cwd,branch,head,"
            "worktree_sha256,note,status,created,updated,ownership_task_id,owned_worktree "
            "FROM recoveries ORDER BY id"
        ).fetchall(), [
            ("r1", "s1", "t1", "f1", "s1", "o1", "/w1", "b1", "h1", "d1", "n1",
             "authorized", 1.0, 1.0, "", ""),
            ("r2", "s2", "t2", "f2", "s2", "o2", "/w2", "b2", "h2", "d2", "n2",
             "claimed", 2.0, 2.0, "", ""),
            ("r3", "s3", "t3", "f3", "s3", "o3", "/w3", "b3", "h3", "d3", "n3",
             "completed", 3.0, 3.0, "", ""),
        ])
        indexes = {row[0] for row in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='recoveries'")}
        self.assertTrue({"recoveries_active_owner", "recoveries_active_cwd",
                         "recoveries_active_worktree", "recoveries_active_turn"} <= indexes)
        self.assertIsNotNone(recovery.recovery_for_successor(migrated, "o1", ("authorized",)))
        self.assertIsNotNone(recovery.recovery_for_successor(migrated, "o2", ("claimed",)))
        self.assertIsNone(migrated.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recoveries_legacy'"
        ).fetchone())

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

    def test_minimax_recovery_verifies_selected_model_and_rejects_provider_switch(self):
        model = "opencode-go/minimax-m3.1-flash-free"
        self.slot["model"] = model
        session = paths.OPENCODE / self.sid
        for path in (session / "current.json", session / "turns" / self.turn / "state.json"):
            data = json.loads(path.read_text())
            data["model"] = model
            path.write_text(json.dumps(data))
        self.slot["model"] = "opencode/minimax-m3.1-flash-free"
        with self.assertRaisesRegex(ValueError, "identity"):
            self.authorize()
        self.slot["model"] = model
        self.assertEqual(self.authorize()["status"], "authorized")

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

    def shared_checkout_slot(self):
        """A second worker whose configured, structured CWD is the owned checkout."""
        shared = dict(self.slot, sessionId="ses_shared_checkout",
                      cwd=str(self.cwd.resolve()), sessionCwd=str(self.cwd.resolve()),
                      databasePath=str(self.root / "shared-native.db"))
        with sqlite3.connect(shared["databasePath"]):
            pass
        self.slot_patch.stop()
        self.slot_patch = patch.object(paths, "opencode_slots",
                                       return_value=[self.slot, shared])
        self.slot_patch.start()
        self.addCleanup(self.slot_patch.stop)
        return shared

    def test_shared_checkout_conflict_when_recovery_authorized_first(self):
        # Admission order 1: recovery owns the checkout, then a "continue" task
        # from a worker configured at that same checkout must be rejected.
        shared = self.shared_checkout_slot()
        self.authorize()
        task = {"id": "shared-successor", "sessionId": shared["sessionId"],
                "rootAuthorized": True, "message": "continue"}
        with patch.object(paths, "all_slots", return_value=[self.slot, shared]):
            with self.assertRaisesRegex(ValueError, "conflicts with active recovery"):
                control.enqueue(self.db, [task])
        self.assertFalse(self.queue.exists())
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM tasks WHERE id='shared-successor'").fetchone()[0], 0)

    def test_shared_checkout_conflict_when_shared_task_enqueued_first(self):
        # Admission order 2: a "continue" task from a worker configured at the
        # checkout is queued first, so authorization must refuse exclusive ownership.
        shared = self.shared_checkout_slot()
        task = {"id": "shared-successor", "sessionId": shared["sessionId"],
                "rootAuthorized": True, "message": "continue"}
        with patch.object(paths, "all_slots", return_value=[self.slot, shared]):
            control.enqueue(self.db, [task])
        with self.assertRaisesRegex(ValueError, "Another queued task owns"):
            self.authorize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_shared_checkout_conflict_blocks_queue_import(self):
        shared = self.shared_checkout_slot()
        self.authorize()
        self.queue.write_text(json.dumps({"author": "root", "tasks": [
            {"id": "shared-import", "sessionId": shared["sessionId"],
             "rootAuthorized": True, "message": "continue"}]}) + "\n")
        with self.assertRaisesRegex(ValueError, "conflicts with active recovery"):
            manager.import_queue(self.db,
                                 {self.sid: self.slot, shared["sessionId"]: shared},
                                 self.queue)
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM tasks WHERE id='shared-import'").fetchone()[0], 0)

    def test_shared_checkout_conflict_blocks_recovery_dispatch(self):
        self.authorize()
        task = {"id": self.successor, "previousTaskId": self.failed_task,
                "recoveryId": "recovery-1", "rootAuthorized": True, "message": "continue"}
        with patch.object(paths, "all_slots", return_value=[self.slot]):
            control.enqueue(self.db, [task])
        manager.import_queue(self.db, {self.sid: self.slot}, self.queue)
        # A competing worker already runs in the owned checkout and says only "continue".
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        ("shared-rival", self.other_sid, str(self.cwd.resolve()),
                         json.dumps({"message": "continue"}), "running", 0))
        self.db.commit()
        sent = []
        slots = {self.sid: self.slot, self.other_sid: self.other_slot}
        states = {self.sid: {"state": "error", "cwd": str(self.native_cwd)},
                  self.other_sid: {"state": "working", "cwd": str(self.cwd.resolve())}}
        dispatch.dispatch(self.db, slots, states,
                          lambda *args, **kwargs: sent.append(kwargs["task_id"]))
        self.assertEqual(sent, [])
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "queued")

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

    # --- retirement / reconciliation --------------------------------------
    def enqueue_successor(self):
        task = {"id": self.successor, "previousTaskId": self.failed_task,
                "recoveryId": "recovery-1", "rootAuthorized": True, "message": "continue"}
        with patch.object(paths, "all_slots", return_value=[self.slot]):
            control.enqueue(self.db, [task])
        manager.import_queue(self.db, {self.sid: self.slot}, self.queue)

    def mark_claimed_uncertain(self, state="uncertain"):
        self.db.execute("UPDATE tasks SET state=? WHERE id=?", (state, self.successor))
        self.db.execute("UPDATE recoveries SET status='claimed' WHERE id='recovery-1'")
        self.db.commit()

    def write_successor_turn(self, state, identity=None, turn="turn_successor"):
        turn_dir = paths.OPENCODE / self.sid / "turns" / turn
        turn_dir.mkdir(parents=True, exist_ok=True)
        payload = {"turn": turn, "taskId": self.successor, "sessionId": self.sid,
                   "cwd": str(self.native_cwd), "model": opencode.MODEL, "state": state}
        if identity is not None:
            payload["processIdentity"] = identity
        if state in {"stopped", "error", "done", "completed", "blocked"}:
            payload["cleanup"] = "confirmed"
        (turn_dir / "state.json").write_text(json.dumps(payload))
        (paths.OPENCODE / self.sid / "current.json").write_text(json.dumps(
            {"turn": turn, "taskId": self.successor, "sessionId": self.sid,
             "cwd": str(self.native_cwd), "model": opencode.MODEL}))
        return turn

    def test_queued_cancel_retires_authorization_atomically(self):
        self.authorize()
        self.enqueue_successor()
        failed_before = self.db.execute("SELECT * FROM tasks WHERE id=?",
                                        (self.failed_task,)).fetchone()
        state_before = (paths.OPENCODE / self.sid / "turns" / self.turn / "state.json").read_bytes()
        result_before = (paths.OPENCODE / self.sid / "turns" / self.turn / "result.json").read_bytes()
        result = ops.cancel(self.db, self.successor, "cancelled before dispatch")
        self.assertEqual(result, {"taskId": self.successor,
                                  "action": {"queuedCancelled": True,
                                             "recoveryRetired": ["recovery-1"]}})
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "cancelled")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "retired")
        # The failed task and its failed turn are preserved verbatim, never replayed.
        self.assertEqual(self.db.execute("SELECT * FROM tasks WHERE id=?",
                                         (self.failed_task,)).fetchone(), failed_before)
        self.assertEqual((paths.OPENCODE / self.sid / "turns" / self.turn /
                          "state.json").read_bytes(), state_before)
        self.assertEqual((paths.OPENCODE / self.sid / "turns" / self.turn /
                          "result.json").read_bytes(), result_before)
        kinds = {row[0] for row in self.db.execute("SELECT kind FROM events").fetchall()}
        self.assertIn("recovery_retired", kinds)
        self.assertIn("task_cancelled", kinds)

    def test_safe_replacement_after_retirement_shares_the_failed_turn(self):
        self.authorize()
        self.enqueue_successor()
        ops.cancel(self.db, self.successor, "cancel the first successor")
        snap = recovery.worktree_snapshot(self.cwd)
        replacement = recovery.authorize_opencode(
            self.db, session_id=self.sid, failed_task_id=self.failed_task, turn=self.turn,
            recovery_id="recovery-2", successor_task_id="successor-task-2",
            ownership_task_id=self.failed_task, owned_worktree=str(self.cwd.resolve()),
            branch=snap["branch"], head=snap["head"], worktree_sha256=snap["worktreeSha256"],
            note="safe replacement after retirement")
        self.assertEqual(replacement["status"], "authorized")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "retired")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-2'")
                         .fetchone()[0], "authorized")
        self.assertEqual(self.db.execute("SELECT failed_task_id,failed_turn FROM recoveries "
                                         "WHERE id='recovery-2'").fetchone(),
                         (self.failed_task, self.turn))

    def test_uncertain_resolution_is_fenced_by_stopped_native_execution(self):
        self.authorize()
        self.enqueue_successor()
        self.mark_claimed_uncertain()
        identity = {"pid": 4242, "pgid": 4242, "start": [1, 2], "executable": "/usr/bin/opencode"}
        self.write_successor_turn("working", identity=identity)
        with patch.object(opencode, "identity_matches", return_value=True):
            with self.assertRaisesRegex(ValueError, "not verified stopped"):
                ops.resolve_task(self.db, self.successor, "cancelled", "manual resolution")
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "uncertain")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "claimed")
        self.write_successor_turn("stopped")   # no live identity, cleanup confirmed
        resolved = ops.resolve_task(self.db, self.successor, "cancelled", "manual resolution")
        self.assertEqual(resolved["state"], "cancelled")
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "cancelled")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "reconciled")

    def test_explicit_reconcile_releases_sent_successor_only_after_proof(self):
        self.authorize()
        self.enqueue_successor()
        self.mark_claimed_uncertain(state="sent")
        identity = {"pid": 99, "pgid": 99, "start": [3, 4], "executable": "/usr/bin/opencode"}
        self.write_successor_turn("working", identity=identity)
        with patch.object(opencode, "identity_matches", return_value=True):
            with self.assertRaisesRegex(ValueError, "not verified stopped"):
                recovery.reconcile_successor(self.db, self.successor, "operator evidence")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "claimed")
        self.write_successor_turn("stopped")
        result = recovery.reconcile_successor(self.db, self.successor, "operator evidence")
        self.assertTrue(result["reconciled"])
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "reconciled")
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "cancelled")

    def test_reconcile_refuses_dispatching_successor_with_old_failed_pointer(self):
        self.authorize()
        self.enqueue_successor()
        attempted = []

        def sender(*args, **kwargs):
            # dispatch has committed the successor claim, but opencode.send has
            # not yet replaced current.json; it still points at the old failed turn.
            with self.assertRaisesRegex(ValueError, "dispatching"):
                recovery.reconcile_successor(self.db, self.successor, "operator evidence")
            attempted.append(kwargs["task_id"])

        dispatch.dispatch(self.db, {self.sid: self.slot},
                          {self.sid: {"state": "error", "cwd": str(self.native_cwd)}}, sender)
        self.assertEqual(attempted, [self.successor])
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "sent")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "claimed")

    def test_reconcile_requires_exact_successor_turn_and_present_pointer(self):
        self.authorize()
        self.enqueue_successor()
        self.mark_claimed_uncertain()
        with self.assertRaisesRegex(ValueError, "successor task"):
            recovery.reconcile_successor(self.db, self.successor, "operator evidence")
        (paths.OPENCODE / self.sid / "current.json").unlink()
        with self.assertRaisesRegex(ValueError, "pointer is unavailable"):
            recovery.reconcile_successor(self.db, self.successor, "operator evidence")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "claimed")

    def test_reconcile_cannot_pass_while_session_dispatch_lock_is_held(self):
        self.authorize()
        self.enqueue_successor()
        self.mark_claimed_uncertain()
        lock_path = paths.OPENCODE / self.sid / "dispatch.lock"
        child_code = (
            "import fcntl,sys; f=open(sys.argv[1], 'a'); "
            "fcntl.flock(f.fileno(), fcntl.LOCK_EX); print('locked', flush=True); "
            "sys.stdin.read()"
        )
        child = subprocess.Popen([sys.executable, "-c", child_code, str(lock_path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(RuntimeError, "already has a Fleet turn"):
                recovery.reconcile_successor(self.db, self.successor, "operator evidence")
        finally:
            child.stdin.close()
            child.wait(timeout=5)
            child.stdout.close()
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "claimed")

    def _assert_cancelled_successor_reconciles(self, state):
        self.authorize()
        self.enqueue_successor()
        self.mark_claimed_uncertain(state=state)
        self.write_successor_turn("stopped")
        with patch.object(paths, "all_slots", return_value=[self.slot]), \
                patch.object(opencode, "cancel", return_value={"stopped": True}):
            cancelled = ops.cancel(self.db, self.successor,
                                   "operator stopped the recovery successor")
        self.assertEqual(cancelled["taskId"], self.successor)
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "cancelled")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "claimed")

        result = recovery.reconcile_successor(
            self.db, self.successor, "verified stopped successor")
        self.assertTrue(result["reconciled"])
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "cancelled")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "reconciled")

    def test_cancelled_sent_successor_reconciles_after_confirmed_cleanup(self):
        self._assert_cancelled_successor_reconciles("sent")

    def test_cancelled_running_successor_reconciles_after_confirmed_cleanup(self):
        self._assert_cancelled_successor_reconciles("running")

    def test_cancelled_successor_with_unconfirmed_cleanup_keeps_reservation(self):
        self.authorize()
        self.enqueue_successor()
        self.mark_claimed_uncertain(state="cancelled")
        self.write_successor_turn("stopped")
        state_path = (paths.OPENCODE / self.sid / "turns" / "turn_successor" / "state.json")
        native_state = json.loads(state_path.read_text())
        native_state["cleanup"] = "uncertain"
        state_path.write_text(json.dumps(native_state))
        with self.assertRaisesRegex(ValueError, "cleanup is not confirmed"):
            recovery.reconcile_successor(self.db, self.successor, "cleanup was not verified")
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.successor,)).fetchone()[0], "cancelled")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "claimed")

    def test_retirement_keeps_other_reservations_and_gates_intact(self):
        self.authorize()
        self.enqueue_successor()
        self.db.execute("INSERT INTO recoveries (id,failed_sid,failed_turn,failed_task_id,"
                        "owner_sid,owner_task_id,ownership_task_id,cwd,owned_worktree,branch,"
                        "head,worktree_sha256,note,status,created,updated) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("unrelated", "ses_other", "turn-other", "failed-other", "ses_other",
                         "successor-other", "ownership-other", str(self.other_native_cwd),
                         str(self.other_native_cwd), "b", "h", "d", "n", "authorized", 1.0, 1.0))
        self.db.commit()
        paths.PAUSE.write_text("paused\n")
        self.slot["dispatchEnabled"] = False
        ops.cancel(self.db, self.successor, "cancel while other holds exist")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='recovery-1'")
                         .fetchone()[0], "retired")
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='unrelated'")
                         .fetchone()[0], "authorized")     # unrelated reservation untouched
        self.assertTrue(paths.PAUSE.exists())              # global pause untouched
        self.assertFalse(self.slot["dispatchEnabled"])     # per-worker hold untouched


if __name__ == "__main__":
    unittest.main()
