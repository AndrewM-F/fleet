"""A genuine direct turn that predates the task ledger can own a recovery."""
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

from fleet import opencode, paths, recovery, store  # noqa: E402

MODEL = "opencode-go/minimax-m3.1-flash-free"
NATIVE_MODEL = json.dumps({"id": "minimax-m3.1-flash-free", "providerID": "opencode-go"})
NOTE = "the original direct turn produced this branch before the Fleet task ledger existed"


class DirectTurnOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.native_cwd = self.root / "native-worker"
        self.native_cwd.mkdir()
        self.worktree = self.root / "owned-worktree"
        self.worktree.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "lane/direct", str(self.worktree)], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "config", "user.email", "fleet@test"],
                       check=True)
        subprocess.run(["git", "-C", str(self.worktree), "config", "user.name", "Fleet Test"],
                       check=True)
        (self.worktree / "direct.txt").write_text("direct turn output\n")
        subprocess.run(["git", "-C", str(self.worktree), "add", "direct.txt"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "commit", "-qm", "direct turn"], check=True)
        self.initial_head = subprocess.run(
            ["git", "-C", str(self.worktree), "rev-parse", "HEAD"], check=True,
            capture_output=True).stdout.decode().strip()
        (self.worktree / "later.txt").write_text("later owner state\n")
        subprocess.run(["git", "-C", str(self.worktree), "add", "later.txt"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "commit", "-qm", "later work"], check=True)
        self.sid = "ses_direct_turn_test"
        self.other_sid = "ses_direct_turn_other"
        self.turn = "turn_failed_direct"
        self.failed_task = "failed-direct-task"
        self.successor = "successor-direct-task"
        self.started_at = 1792480175
        self.native_db = self.root / "native.db"
        with sqlite3.connect(self.native_db) as native:
            native.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, project_id TEXT, "
                           "model TEXT, idle_outcome TEXT, time_created INTEGER, "
                           "time_updated INTEGER)")
            native.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
                           (self.sid, str(self.worktree.resolve()), "project-test", NATIVE_MODEL,
                            "succeeded", self.started_at * 1000, self.started_at * 1000 + 5000))
            native.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, "
                           "seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")
            native.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                           ("m1", self.sid, "assistant", 1, self.started_at * 1000,
                            self.started_at * 1000, "{}"))
        self.slot = {"sessionId": self.sid, "cwd": str(self.native_cwd),
                     "sessionCwd": str(self.native_cwd), "databasePath": str(self.native_db),
                     "model": MODEL, "provider": "opencode", "dispatchEnabled": True}
        self.data = self.root / "data"
        (self.data / "recovery").mkdir(parents=True, exist_ok=True)
        self.prepare_failure()
        self.db = store.database(self.root / "fleet.sqlite")
        # The failed managed turn is a Fleet task: it was dispatched. The
        # original direct turn that owns the work has no task row at all.
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (self.failed_task, self.sid, str(self.native_cwd.resolve()),
                         json.dumps({"message": "failed managed turn",
                                     "sessionId": self.sid, "rootAuthorized": True}),
                         "finished", 1))
        self.db.commit()
        self.addCleanup(self.db.close)
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("OPENCODE", self.data / "opencode"), ("LOGS", self.root / "logs"),
                            ("BIN", self.root / "bin" / "fleet"),
                            ("RECOVERY", self.data / "recovery"),
                            ("QUEUE", self.data / "queue.json"),
                            ("QUEUE_LOCK", self.data / "queue.lock"),
                            ("PAUSE", self.data / "dispatch.paused")):
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        roster = patch.object(paths, "opencode_slots", return_value=[self.slot])
        roster.start()
        self.addCleanup(roster.stop)
        executable = patch.object(paths, "executable", return_value="/usr/bin/opencode")
        executable.start()
        self.addCleanup(executable.stop)
        self.receipt = self.data / "recovery" / "direct-turn-w8.json"
        self.write_receipt()

    def prepare_failure(self, identity=None):
        session = self.data / "opencode" / self.sid
        turn_dir = session / "turns" / self.turn
        turn_dir.mkdir(parents=True, exist_ok=True)
        launch = {"turn": self.turn, "taskId": self.failed_task, "sessionId": self.sid,
                  "cwd": str(self.native_cwd.resolve()), "model": MODEL}
        (session / "current.json").write_text(json.dumps(launch))
        state = dict(launch, state="error", cleanup="confirmed",
                     error="OpenCode turn did not produce a verified native completion",
                     finishedAt=100)
        result = dict(taskId=self.failed_task, sessionId=self.sid, cwd=launch["cwd"],
                      state="error", cleanup="confirmed", error=state["error"], toolFailures=[],
                      finishedAt=100)
        if identity:
            result["managedProcessIdentity"] = identity
        (turn_dir / "state.json").write_text(json.dumps(state))
        (turn_dir / "result.json").write_text(json.dumps(result))

    def write_receipt(self, **overrides):
        document = {"schemaVersion": recovery.DIRECT_TURN_RECEIPT_VERSION,
                    "originalSession": self.sid, "ownedWorktree": str(self.worktree.resolve()),
                    "branch": "lane/direct", "initialHead": self.initial_head,
                    "startedAt": self.started_at, "note": NOTE}
        document.update(overrides)
        self.receipt.write_text(json.dumps(document, indent=2) + "\n")
        return document

    def inputs(self, **overrides):
        snapshot = recovery.worktree_snapshot(self.worktree)
        values = {"session_id": self.sid, "failed_task_id": self.failed_task, "turn": self.turn,
                  "recovery_id": "recovery-direct-1", "successor_task_id": self.successor,
                  "ownership_task_id": "", "owned_worktree": str(self.worktree.resolve()),
                  "branch": snapshot["branch"], "head": snapshot["head"],
                  "worktree_sha256": snapshot["worktreeSha256"], "note": "direct turn ownership",
                  "direct_turn_receipt": str(self.receipt)}
        values.update(overrides)
        return values

    def authorize(self, **overrides):
        return recovery.authorize_opencode(self.db, **self.inputs(**overrides))

    def recovery_row(self, recovery_id="recovery-direct-1"):
        names = [item[0] for item in self.db.execute("SELECT * FROM recoveries LIMIT 0").description]
        row = self.db.execute("SELECT * FROM recoveries WHERE id=?", (recovery_id,)).fetchone()
        return dict(zip(names, row))

    def test_direct_turn_receipt_authorizes_a_recovery_without_an_owner_task(self):
        result = self.authorize()
        self.assertEqual(result["status"], "authorized")
        record = self.recovery_row()
        self.assertEqual(record["ownership_task_id"], "")
        self.assertEqual(record["adoption_id"], "")
        self.assertEqual(record["direct_turn_receipt"], str(self.receipt.resolve()))
        self.assertEqual(len(record["direct_turn_sha256"]), 64)
        # No Fleet task row is created or rewritten for the ownership: only the
        # failed managed turn's own row exists.
        self.assertEqual(self.db.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?",
                                         (self.failed_task,)).fetchone()[0], "finished")
        event = json.loads(self.db.execute(
            "SELECT body FROM events WHERE kind='opencode_recovery_prepared'").fetchone()[0])
        self.assertEqual(event["direct_turn_receipt"], str(self.receipt.resolve()))
        self.assertEqual(event["direct_turn_evidence"]["nativeSession"], self.sid)
        self.assertEqual(event["direct_turn_evidence"]["nativeIdleOutcome"], "succeeded")
        self.assertEqual(event["direct_turn_evidence"]["nativeAssistantMessages"], 1)

    def test_wrong_original_session_is_refused(self):
        self.write_receipt(originalSession=self.other_sid)
        with self.assertRaisesRegex(ValueError, "different original session"):
            self.authorize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_wrong_canonical_worktree_is_refused(self):
        other = self.root / "other-worktree"
        other.mkdir()
        self.write_receipt(ownedWorktree=str(other.resolve()))
        with self.assertRaisesRegex(ValueError, "differs from the recorded owned worktree"):
            self.authorize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_wrong_branch_is_refused(self):
        self.write_receipt(branch="lane/other")
        with self.assertRaisesRegex(ValueError, "branch differs from the recorded branch"):
            self.authorize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_wrong_initial_head_is_refused(self):
        self.write_receipt(initialHead="0" * 40)
        with self.assertRaisesRegex(ValueError, "initial head is not an ancestor"):
            self.authorize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_tampered_receipt_is_refused_at_consumption(self):
        self.authorize()
        record = self.recovery_row()
        document = json.loads(self.receipt.read_text())
        document["initialHead"] = "0" * 40
        self.receipt.write_text(json.dumps(document, indent=2) + "\n")
        with self.assertRaisesRegex(ValueError, "changed after recovery authorization"):
            recovery.validate_owned_worktree(self.db, record, [self.slot])
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 1)

    def test_missing_receipt_is_refused(self):
        self.receipt.unlink()
        with self.assertRaisesRegex(ValueError, "receipt is missing"):
            self.authorize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_receipt_outside_the_fleet_recovery_directory_is_refused(self):
        outside = self.root / "outside.json"
        outside.write_text(self.receipt.read_text())
        with self.assertRaisesRegex(ValueError, "must live in the fleet recovery directory"):
            self.authorize(direct_turn_receipt=str(outside))
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_live_failed_turn_is_refused(self):
        self.prepare_failure(identity={"pid": 4242, "pgid": 4242, "start": [1],
                                       "executable": "/usr/bin/opencode"})
        with patch.object(opencode, "identity_matches", return_value=True):
            with self.assertRaisesRegex(ValueError, "still live"):
                self.authorize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_duplicate_successor_for_the_same_failed_turn_is_refused(self):
        self.authorize()
        with self.assertRaisesRegex(ValueError, "already has an active recovery record"):
            self.authorize(recovery_id="recovery-direct-2", successor_task_id="successor-direct-2")
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 1)

    def test_ownership_sources_are_exclusive(self):
        with self.assertRaisesRegex(ValueError, "exactly one ownership source"):
            self.authorize(ownership_task_id="some-owner-task")
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_native_session_without_a_completed_turn_is_refused(self):
        with sqlite3.connect(self.native_db) as native:
            native.execute("UPDATE session_v2 SET idle_outcome='aborted' WHERE id=?", (self.sid,))
        with self.assertRaisesRegex(ValueError, "did not complete successfully"):
            self.authorize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)

    def test_native_session_in_another_directory_is_refused(self):
        with sqlite3.connect(self.native_db) as native:
            native.execute("UPDATE session_v2 SET directory=? WHERE id=?",
                           (str(self.native_cwd.resolve()), self.sid))
        with self.assertRaisesRegex(ValueError, "differs from the native session directory"):
            self.authorize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
