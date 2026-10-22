"""A stale native identity cannot be adopted, and an adoption proves its session.

CP #12 (Fleet issue #12) covers the stale identity and the ownership-adoption
recovery path. The fixture below owns a disposable native session identity: the
session row the native side records, next to a managed turn pointer, its turn
receipts and a Fleet task ledger. Every proof therefore runs against a real
identity row instead of a patched one, and a test can make that identity stale
by removing the row or by pointing it at another directory.

Nothing here touches a live fleet, a live configuration, a live native database
or a credential: the root, the roster, the ledger, the native identity and the
checkout are all disposable fixtures under a temporary directory.
"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import opencode, opencode_lifecycle, paths, recovery, store  # noqa: E402


class StaleIdentityAdoptionTests(unittest.TestCase):
    """The ownership adoption proves the native session it names."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "worktree"
        self.cwd.mkdir()
        self.native_cwd = self.root / "native-worker"
        self.native_cwd.mkdir()
        self.other_cwd = self.root / "other-worker"
        self.other_cwd.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "repair", str(self.cwd)], check=True)
        subprocess.run(["git", "-C", str(self.cwd), "config", "user.email", "fleet@test"],
                       check=True)
        subprocess.run(["git", "-C", str(self.cwd), "config", "user.name", "Fleet Test"],
                       check=True)
        (self.cwd / "partial.txt").write_text("preserve this partial work\n")
        subprocess.run(["git", "-C", str(self.cwd), "add", "partial.txt"], check=True)
        subprocess.run(["git", "-C", str(self.cwd), "commit", "-qm", "fixture"], check=True)
        self.sid = "ses_stale_identity"
        self.turn = "turn_failed_1"
        self.failed_task = "failed-task"
        self.owner_task = "owner-task"
        self.successor = "successor-task"
        self.native_db = self.root / "native.db"
        self.slot = {"sessionId": self.sid, "cwd": str(self.native_cwd.resolve()),
                     "sessionCwd": str(self.native_cwd.resolve()),
                     "databasePath": str(self.native_db), "model": opencode.MODEL,
                     "provider": "opencode", "dispatchEnabled": True}
        self.write_native_identity(self.native_cwd)
        self.db_path = self.root / "fleet.sqlite"
        self.db = store.database(self.db_path)
        self.addCleanup(self.db.close)
        self.addCleanup(self.tmp.cleanup)
        self.data = self.root / "data"
        self.queue = self.data / "queue.json"
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        for name, value in (("OPENCODE", self.data / "opencode"), ("LOGS", self.root / "logs"),
                            ("BIN", self.root / "bin" / "fleet"), ("QUEUE", self.queue),
                            ("QUEUE_LOCK", self.data / "queue.lock"),
                            ("PAUSE", self.data / "dispatch.paused"), ("DATA", self.data)):
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config_path = self.root / "synthetic-fleet.json"
        # A synthetic CLI executable: the lifecycle proofs resolve it, and nothing
        # in this module ever runs it.
        self.fake = self.root / "fake-opencode"
        self.fake.write_text("#!/bin/sh\nexit 0\n")
        self.fake.chmod(0o755)
        self.config_path.write_text(json.dumps(
            {"executables": {"opencode": str(self.fake)},
             "pi": [], "claude": [], "opencode": [self.slot]}))
        config_patcher = patch.object(paths, "CONFIG", self.config_path)
        config_patcher.start()
        self.addCleanup(config_patcher.stop)
        self.prepare_failure()
        for task_id, message in ((self.failed_task, "the failed managed turn"),
                                  (self.owner_task,
                                   "create the isolated worktree and branch for this work, "
                                   "then continue")):
            self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                            (task_id, self.sid, str(self.native_cwd.resolve()),
                             json.dumps({"message": message, "sessionId": self.sid,
                                         "rootAuthorized": True}), "finished", 1))
        self.db.commit()

    # --- the disposable owned identity and turn ---------------------------
    def write_native_identity(self, directory, model=opencode.MODEL, session_id=None):
        """Record the native session identity this fixture owns."""
        with sqlite3.connect(self.native_db) as native:
            native.execute("CREATE TABLE IF NOT EXISTS session_v2 (id TEXT, directory TEXT, "
                           "project_id TEXT, model TEXT)")
            native.execute("DELETE FROM session_v2")
            native.execute("INSERT INTO session_v2 VALUES (?,?,?,?)",
                           (session_id or self.sid, str(Path(directory).resolve()),
                            "project-test", model))

    def delete_native_identity_row(self):
        """Make the native identity stale: the row the roster names is gone.

        The table stays. The native database can still be read, and what makes
        the identity stale is that the row it no longer holds is not the row
        the roster names. That is the fact these proofs are about - an
        identity that was read and does not match - and not the absence of a
        database, which is a different case with a different answer.
        """
        with sqlite3.connect(self.native_db) as native:
            native.execute("DELETE FROM session_v2 WHERE id=?", (self.sid,))

    def prepare_failure(self):
        """The managed turn that failed, with its pointer and its receipts."""
        session = paths.OPENCODE / self.sid
        turn_dir = session / "turns" / self.turn
        turn_dir.mkdir(parents=True, exist_ok=True)
        launch = {"turn": self.turn, "taskId": self.failed_task, "sessionId": self.sid,
                  "cwd": str(self.native_cwd.resolve()), "model": opencode.MODEL}
        (session / "current.json").write_text(json.dumps(launch))
        state = dict(launch, state="error", cleanup="confirmed",
                     error="OpenCode turn did not produce a verified native completion",
                     finishedAt=100)
        result = dict(turn=self.turn, taskId=self.failed_task, sessionId=self.sid,
                      cwd=str(self.native_cwd.resolve()), expectedModel=opencode.MODEL,
                      state="error", cleanup="confirmed", error=state["error"], toolFailures=[],
                      finishedAt=100,
                      managedProcessIdentity={"pid": 9999999, "pgid": 9999999, "start": [1],
                                              "executable": "/usr/bin/opencode"})
        (turn_dir / "state.json").write_text(json.dumps(state))
        (turn_dir / "result.json").write_text(json.dumps(result))

    # --- the two calls under test ------------------------------------------
    def adoption_inputs(self, **overrides):
        snapshot = recovery.worktree_snapshot(self.cwd)
        values = {"adoption_id": "adoption-1", "owner_task_id": self.owner_task,
                  "session_id": self.sid, "failed_task_id": self.failed_task,
                  "turn": self.turn, "owned_worktree": str(self.cwd.resolve()),
                  "branch": snapshot["branch"], "head": snapshot["head"],
                  "worktree_sha256": snapshot["worktreeSha256"],
                  "decision": "adopt this checkout as the worker's explicitly owned worktree",
                  "evidence_reference": "operator-handoff:W4-2026-10-21T09:00Z",
                  "ownership_evidence": ("W4 handoff: the operator confirmed this isolated "
                                         "checkout and branch are the ones the owner task used "
                                         "for this worker, and that the checkout is unchanged."),
                  "note": "ownership adoption for the original owner task"}
        values.update(overrides)
        return values

    def adopt(self, **overrides):
        return recovery.adopt_ownership(self.db, **self.adoption_inputs(**overrides))

    def authorize(self, **overrides):
        snapshot = recovery.worktree_snapshot(self.cwd)
        values = {"session_id": self.sid, "failed_task_id": self.failed_task, "turn": self.turn,
                  "recovery_id": "recovery-1", "successor_task_id": self.successor,
                  "ownership_task_id": self.owner_task, "adoption_id": "adoption-1",
                  "owned_worktree": str(self.cwd.resolve()), "branch": snapshot["branch"],
                  "head": snapshot["head"], "worktree_sha256": snapshot["worktreeSha256"],
                  "note": "confirmed failed completion and preserved partial checkout"}
        values.update(overrides)
        return recovery.authorize_opencode(self.db, **values)

    # --- the proofs ---------------------------------------------------------
    def test_the_adoption_proves_the_native_identity_and_records_the_evidence(self):
        adopted = self.adopt()
        self.assertEqual(adopted, {"adoptionId": "adoption-1", "alreadyRecorded": False})
        record = recovery.adoption_record(self.db, "adoption-1")
        self.assertEqual(record["owner_sid"], self.sid)
        self.assertEqual(record["owner_task_id"], self.owner_task)

    def test_a_stale_native_identity_is_refused_and_records_nothing(self):
        self.delete_native_identity_row()
        with self.assertRaisesRegex(ValueError, "native session identity"):
            self.adopt()
        self.assertIsNone(recovery.adoption_record(self.db, "adoption-1"))
        kinds = [row[0] for row in self.db.execute("SELECT kind FROM events ORDER BY id")]
        self.assertNotIn("recovery_ownership_adopted", kinds)
        # The failed task, the owner task and the turn history are untouched.
        self.assertEqual(self.db.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)
        self.assertTrue((paths.OPENCODE / self.sid / "current.json").is_file())

    def test_a_native_identity_that_points_elsewhere_is_refused(self):
        self.write_native_identity(self.other_cwd)
        with self.assertRaisesRegex(ValueError, "native session identity"):
            self.adopt()
        self.assertIsNone(recovery.adoption_record(self.db, "adoption-1"))

    def test_a_session_that_went_stale_after_the_adoption_reserves_nothing(self):
        self.adopt()
        self.delete_native_identity_row()
        with self.assertRaisesRegex(ValueError, "native session identity"):
            self.authorize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM recoveries").fetchone()[0], 0)
        # No successor task, no queue entry and no reservation of any kind.
        self.assertEqual(self.db.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)
        self.assertFalse(self.queue.exists())

    def test_the_proved_identity_authorizes_the_adopted_recovery(self):
        self.adopt()
        result = self.authorize()
        self.assertEqual(result["status"], "authorized")
        row = self.db.execute("SELECT status,adoption_id,owner_sid FROM recoveries "
                               "WHERE id='recovery-1'").fetchone()
        self.assertEqual(row, ("authorized", "adoption-1", self.sid))
        # The owner task's own message is never rewritten.
        self.assertEqual(self.db.execute("SELECT body FROM tasks WHERE id=?",
                                         (self.owner_task,)).fetchone()[0],
                         json.dumps({"message": "create the isolated worktree and branch for this "
                                               "work, then continue", "sessionId": self.sid,
                                     "rootAuthorized": True}))


class _UnreadableDatabase:
    """A native database read that fails the way a locked or damaged file fails.

    The failure is injected at the read boundary instead of being produced by
    the file's permissions. A permission bit does not prove unreadability when
    the suite runs privileged - a privileged reader ignores it - so a chmod
    could not carry the claim, and the proof would depend on who ran it. Here
    the read fails for everybody, and the real verification code runs above
    the injection, so the classification it makes is the one under test.
    """

    Error = sqlite3.Error

    @staticmethod
    def connect(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")


class StaleIdentityRemovalTests(unittest.TestCase):
    """A stale identity is never a removal shortcut.

    The same disposable native identity the adoption proofs use is here bound to
    a roster entry, so the removal barriers run against a real identity row. An
    identity that was read and does not name the session the roster holds - the
    row is gone, or it points at another directory - is a fact about the
    worker, not an absence of evidence: the operator override covers only an
    identity that cannot be read at all, and it never touches a hold, a task, a
    queue entry, a worktree or the native session.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "worker"
        self.cwd.mkdir()
        self.other_cwd = self.root / "other-worker"
        self.other_cwd.mkdir()
        self.sid = "ses_stale_removal"
        self.native_db = self.root / "native.db"
        self.slot = {"sessionId": self.sid, "cwd": str(self.cwd.resolve()),
                     "sessionCwd": str(self.cwd.resolve()),
                     "databasePath": str(self.native_db), "model": opencode.MODEL,
                     "provider": "opencode", "dispatchEnabled": True}
        self.write_native_identity(self.cwd)
        self.db_path = self.root / "fleet.sqlite"
        self.db = store.database(self.db_path)
        self.addCleanup(self.db.close)
        self.addCleanup(self.tmp.cleanup)
        self.data = self.root / "data"
        self.queue = self.data / "queue.json"
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        for name, value in (("OPENCODE", self.data / "opencode"), ("LOGS", self.root / "logs"),
                            ("BIN", self.root / "bin" / "fleet"), ("QUEUE", self.queue),
                            ("QUEUE_LOCK", self.data / "queue.lock"),
                            ("PAUSE", self.data / "dispatch.paused"), ("DATA", self.data)):
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fake = self.root / "fake-opencode"
        self.fake.write_text("#!/bin/sh\nexit 0\n")
        self.fake.chmod(0o755)
        self.config_path = self.root / "synthetic-fleet.json"
        self.config_path.write_text(json.dumps(
            {"executables": {"opencode": str(self.fake)},
             "pi": [], "claude": [], "opencode": [self.slot]}))
        config_patcher = patch.object(paths, "CONFIG", self.config_path)
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

    # --- the disposable owned identity and the roster -----------------------
    def write_native_identity(self, directory):
        """Record the native session identity this fixture owns."""
        with sqlite3.connect(self.native_db) as native:
            native.execute("CREATE TABLE IF NOT EXISTS session_v2 (id TEXT, directory TEXT, "
                           "project_id TEXT, model TEXT)")
            native.execute("DELETE FROM session_v2")
            native.execute("INSERT INTO session_v2 VALUES (?,?,?,?)",
                           (self.sid, str(Path(directory).resolve()),
                            "project-test", opencode.MODEL))

    def delete_native_identity_row(self):
        """Make the native identity stale: the row the roster names is gone."""
        with sqlite3.connect(self.native_db) as native:
            native.execute("DELETE FROM session_v2 WHERE id=?", (self.sid,))

    def roster(self):
        return paths.config().get("opencode", [])

    def remove(self, **kwargs):
        return opencode_lifecycle.remove_session(self.db, self.sid, note="operator decision",
                                                 **kwargs)

    # --- the proofs ---------------------------------------------------------
    def test_a_stale_native_identity_is_refused_even_with_the_override(self):
        # The native database was read and the row it holds is gone. That is a
        # fact about the worker, so the override does not cover it.
        self.delete_native_identity_row()
        before = self.native_db.read_bytes()
        with self.assertRaises(ValueError) as error:
            self.remove(allow_unverified_native=True)
        self.assertIn("verified", str(error.exception))
        # The roster still holds the worker and the native database is untouched.
        self.assertEqual(len(self.roster()), 1)
        self.assertEqual(self.native_db.read_bytes(), before)

    def test_an_identity_that_points_elsewhere_is_refused_even_with_the_override(self):
        self.write_native_identity(self.other_cwd)
        with self.assertRaises(ValueError) as error:
            self.remove(allow_unverified_native=True)
        self.assertIn("verified", str(error.exception))
        self.assertEqual(len(self.roster()), 1)

    def test_an_identity_that_cannot_be_read_at_all_may_be_removed_with_the_override(self):
        # A hold, a finished task and a root-authored queue entry that belong to
        # other work: the override touches none of them.
        self.db.execute("INSERT INTO task_holds VALUES ('t-other','held','held',1,1)")
        self.db.execute("INSERT INTO tasks VALUES ('t-done','t-other',?,?,?,?)",
                        (str(self.cwd), json.dumps({"message": "m"}), "finished", 1))
        self.db.commit()
        self.queue.write_text(json.dumps({"author": "root",
                                          "tasks": [{"id": "q-1", "sessionId": "ses_other",
                                                     "message": "m"}]}))
        original = self.native_db.read_bytes()
        # The native database cannot be read at all: the identity cannot be
        # read, which is the only case the operator override covers. The read
        # failure is injected at the boundary rather than produced by the
        # file's permissions, so the proof holds whoever runs the suite.
        with patch.object(opencode, "sqlite3", _UnreadableDatabase):
            result = self.remove(allow_unverified_native=True)
        self.assertTrue(result["removed"])
        self.assertTrue(result["nativeSessionKept"])
        self.assertEqual(self.roster(), [])
        # Nothing else is cancelled, released, dropped or deleted.
        self.assertEqual(self.db.execute("SELECT status FROM task_holds WHERE task_id='t-other'")
                         .fetchone()[0], "held")
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id='t-done'").fetchone()[0],
                         "finished")
        self.assertEqual(json.loads(self.queue.read_text())["tasks"],
                         [{"id": "q-1", "sessionId": "ses_other", "message": "m"}])
        self.assertEqual(self.native_db.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
