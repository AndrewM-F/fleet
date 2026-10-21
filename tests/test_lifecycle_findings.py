"""The seven root-reviewed findings, each reproduced and then held closed.

Every test here runs against a temporary root with a synthetic configuration, a
synthetic native session inventory and a fake OpenCode CLI. The live fleet, the
live configuration and the live native database are never touched.
"""
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fleet import (control, manager, model_selection, opencode_lifecycle,  # noqa: E402
                   paths, roster, store)
from test_opencode_lifecycle import FAKE_CLI  # noqa: E402

MODEL = "opencode-go/minimax-m3.1-flash-free"


class FindingSandbox(unittest.TestCase):
    """A temporary fleet root with one configured OpenCode worker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        self.wt = root / "wt" / "repo"
        self.wt.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.wt)], check=True)
        self.native = root / "opencode.db"
        with sqlite3.connect(self.native) as db:
            db.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, project_id TEXT, "
                       "model TEXT)")
            db.execute("INSERT INTO session_v2 VALUES (?,?,?,?)",
                       ("ses_EXISTING000000000001", str(self.wt), "p", MODEL))
        self.existing = "ses_EXISTING000000000001"
        self.config = {"schemaVersion": 2, "worktreeRoot": str(root / "wt"),
                       "executables": {"opencode": str(root / "fake-opencode")},
                       "maxConcurrent": 4, "minQueuedPerWorker": 0, "pi": [], "claude": [],
                       "opencode": [{"sessionId": self.existing, "cwd": str(self.wt),
                                     "sessionCwd": str(self.wt), "databasePath": str(self.native),
                                     "model": MODEL, "provider": "opencode",
                                     "dispatchEnabled": True}]}
        self.control = root / "control.json"
        self.control.write_text(json.dumps({"mode": "ok", "sessionId": "ses_FAKE00000000000001"}))
        self.fake_log = root / "fake-cli.log"
        fake = root / "fake-opencode"
        fake.write_text(FAKE_CLI.format(control=str(self.control), database=str(self.native),
                                        log=str(self.fake_log)))
        fake.chmod(0o755)
        (root / "config").mkdir()
        (root / "config" / "fleet.local.json").write_text(json.dumps(self.config))
        (root / "data").mkdir()
        for name, value in (("ROOT", root), ("CONFIG", root / "config" / "fleet.local.json"),
                            ("DATA", root / "data"), ("LOGS", root / "logs"),
                            ("DB", root / "data" / "fleet.sqlite"),
                            ("QUEUE", root / "data" / "queue.json"),
                            ("QUEUE_LOCK", root / "data" / "queue.lock"),
                            ("MANAGER_LOCK", root / "data" / "manager.lock"),
                            ("OPENCODE", root / "data" / "opencode")):
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.db = store.database(paths.DB)
        self.addCleanup(self.db.close)

    def write_config(self, config):
        paths.CONFIG.write_text(json.dumps(config))

    def digest(self, count=1, target_total=None, enable_dispatch=False, max_concurrent=None):
        """The payload digest the module binds to a request key."""
        request = {"root": str(self.root), "account": str(self.native), "model": MODEL,
                   "cwd": str(self.wt)}
        return opencode_lifecycle._payload_digest(request, count, target_total,
                                                 enable_dispatch, max_concurrent)

    def queue(self, tasks):
        paths.QUEUE.parent.mkdir(parents=True, exist_ok=True)
        paths.QUEUE.write_text(json.dumps({"author": "root", "tasks": tasks}))


class QueueReadinessTests(FindingSandbox):
    """Findings 1 and 2: the queue is never read as empty, and history is not work."""

    def test_an_unreadable_queue_refuses_the_removal(self):
        paths.QUEUE.write_text("{not json")
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing, note="refuse")
        self.assertIn("could not be read", str(error.exception))
        self.assertIn("refused", str(error.exception))
        self.assertEqual(len(paths.config()["opencode"]), 1)

    def test_a_missing_queue_file_is_not_a_refusal(self):
        # No queue file means no queued work, which is a fact, not a failure.
        result = opencode_lifecycle.remove_session(self.db, self.existing, note="remove")
        self.assertTrue(result["removed"])

    def test_imported_history_does_not_block_the_removal(self):
        # A queue entry that is already in the ledger was imported: it is
        # history, not work waiting to arrive.
        self.task("t-old", self.existing, "finished")
        self.queue([{"id": "t-old", "sessionId": self.existing, "message": "m",
                     "rootAuthorized": True}])
        result = opencode_lifecycle.remove_session(self.db, self.existing, note="remove")
        self.assertTrue(result["removed"])
        self.assertEqual(len(paths.config()["opencode"]), 0)

    def test_a_task_still_awaiting_import_blocks_the_removal(self):
        self.queue([{"id": "t-new", "sessionId": self.existing, "message": "m",
                     "rootAuthorized": True}])
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing, note="remove")
        self.assertIn("waiting to be imported", str(error.exception))
        self.assertEqual(len(paths.config()["opencode"]), 1)

    def test_the_drain_reports_a_queue_it_could_not_read(self):
        paths.QUEUE.write_text("{not json")
        result = opencode_lifecycle.drain_session(self.db, self.existing, note="drain")
        self.assertTrue(result["draining"])
        self.assertIn("queue", result["queueUnreadable"])
        self.assertEqual(result["awaitingImport"], [])

    def task(self, task_id, sid, state):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (task_id, sid, str(self.wt), json.dumps({"message": "m"}), state, 1))
        self.db.commit()


class RosterReadTests(FindingSandbox):
    """Finding 3: the enqueue and the import use the roster as it is under the lock."""

    def test_the_enqueue_refuses_a_worker_the_roster_no_longer_has(self):
        # The roster changes while the enqueue is waiting for the writer lock.
        started = threading.Event()
        release = threading.Event()

        def change_the_roster():
            with roster.roster_lock():
                started.set()
                release.wait(10)
                config = paths.config()
                config["opencode"] = []
                paths.write_config(config)

        changer = threading.Thread(target=change_the_roster)
        changer.start()
        started.wait(10)
        outcome = {}

        def enqueue():
            try:
                outcome["result"] = control.enqueue(self.db, [
                    {"id": "t-enq", "sessionId": self.existing, "message": "m",
                     "rootAuthorized": True}])
            except ValueError as error:
                outcome["error"] = str(error)

        worker = threading.Thread(target=enqueue)
        worker.start()
        release.set()
        changer.join(10)
        worker.join(10)
        self.assertIn("error", outcome)
        self.assertIn("not in current roster", outcome["error"])
        self.assertFalse(paths.QUEUE.exists() and json.loads(
            paths.QUEUE.read_text())["tasks"])

    def test_the_import_refuses_a_worker_the_roster_no_longer_has(self):
        self.queue([{"id": "t-imp", "sessionId": self.existing, "message": "m",
                     "rootAuthorized": True}])
        config = paths.config()
        config["opencode"] = []
        self.write_config(config)
        with self.assertRaises(ValueError) as error:
            manager.import_queue(self.db, {self.existing: {"sessionId": self.existing}},
                                 str(paths.QUEUE))
        self.assertIn("removed from the roster", str(error.exception))
        self.assertEqual(self.db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)


class ModelSelectionLockTests(FindingSandbox):
    """Finding 4: the model selection shares the roster writer lock."""

    def test_a_selection_waits_for_the_roster_writer_lock(self):
        started = threading.Event()
        release = threading.Event()
        outcome = {}

        def hold():
            with roster.roster_lock():
                started.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        started.wait(10)

        def select():
            connection = store.database(paths.DB)
            try:
                outcome["result"] = model_selection.apply(
                    None, "opencode/minimax-m3.1-flash-free", self.existing, dry_run=False,
                    db=connection)
            except Exception as error:  # noqa: BLE001 - the failure is the result
                outcome["error"] = str(error)
            finally:
                connection.close()

        worker = threading.Thread(target=select)
        worker.start()
        worker.join(2)
        self.assertNotIn("result", outcome, "the selection did not wait for the writer lock")
        release.set()
        worker.join(10)
        holder.join(10)
        self.assertIn("result", outcome)
        self.assertTrue(outcome["result"]["applied"])

    def test_a_selection_never_overwrites_a_roster_change(self):
        # The configuration changed between the selection's read and its write.
        real = roster.config_digest()
        calls = []

        def stale_after_the_first_call():
            calls.append(1)
            return real if len(calls) == 1 else "a-stale-digest"

        with patch.object(roster, "config_digest", side_effect=stale_after_the_first_call):
            with self.assertRaises(ValueError) as error:
                model_selection.apply(None, "opencode/minimax-m3.1-flash-free", self.existing,
                                      dry_run=False, db=self.db)
        self.assertIn("changed while", str(error.exception))
        self.assertEqual(paths.config()["opencode"][0]["model"], MODEL)


class FinalIntentTests(FindingSandbox):
    """Finding 5: the final intent never overwrites a registered request."""

    def test_the_final_intent_keeps_a_registered_request_registered(self):
        opencode_lifecycle._record_provisioning(
            self.db, "w5-final-001",
            {"model": MODEL, "account": str(self.native), "cwd": str(self.wt),
             "root": str(self.root), "count": 1, "dispatchEnabled": False},
            "registered", "done", payload_digest="digest",
            creator_pid=os.getpid(), keep_registered=True)
        # A final intent arrives for the same request.
        opencode_lifecycle._record_provisioning(
            self.db, "w5-final-001",
            {"model": MODEL, "account": str(self.native), "cwd": str(self.wt),
             "root": str(self.root), "count": 1, "dispatchEnabled": False},
            "creating", json.dumps({"sessions": ["ses_X"]}), payload_digest=self.digest(),
            creator_pid=os.getpid(), keep_registered=True)
        row = opencode_lifecycle._provisioning_row(self.db, "w5-final-001")
        self.assertEqual(row[7], "registered")
        self.assertEqual(json.loads(row[10])["sessions"], ["ses_X"])


class InterruptedCreateTests(FindingSandbox):
    """Finding 6: an interruption with an unreceipted create stays uncertain."""

    def _row(self, key, status, detail=""):
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest,"
            "creator_pid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, MODEL, str(self.native), str(self.wt), str(self.root), 2, 0, status, 1, 1,
             detail, self.digest(2), 0))
        self.db.commit()

    def _receipt(self, key, session_id, status="created"):
        self.db.execute(
            "INSERT INTO opencode_sessions (session_id,request_key,native_id,model,account,cwd,"
            "root,status,created,updated,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, key, session_id, MODEL, str(self.native), str(self.wt), str(self.root),
             status, 1, 1, ""))
        self.db.commit()

    def test_a_pending_receipt_alone_does_not_continue_a_multi_create(self):
        # One of the two sessions was receipted; the second may have been
        # created without a receipt, so the request stays uncertain.
        self._row("w5-int-001", "creating")
        self._receipt("w5-int-001", "ses_FIRST00000000000001")
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       ("ses_FIRST00000000000001", str(self.wt), "p", MODEL))
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.add_sessions(
                self.db, count=2, model=MODEL, cwd=str(self.wt), root=str(self.root),
                request_key="w5-int-001")
        self.assertIn("uncertain", str(error.exception))
        self.assertEqual(len(paths.config()["opencode"]), 1)

    def test_a_complete_recorded_set_continues_safely(self):
        # The attempt recorded every session it was going to create, so the
        # continuation is exact.
        self._row("w5-int-002", "creating", json.dumps(
            {"sessions": ["ses_SECOND0000000000001", "ses_SECOND0000000000002"]}))
        with sqlite3.connect(self.native) as db:
            for identifier in ("ses_SECOND0000000000001", "ses_SECOND0000000000002"):
                db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                           (identifier, str(self.wt), "p", MODEL))
        result = opencode_lifecycle.add_sessions(
            self.db, count=2, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-int-002")
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(paths.config()["opencode"]), 3)


class DryRunWriteTests(FindingSandbox):
    """Finding 7: a dry run writes nothing at all."""

    def test_a_dry_run_with_an_unverified_receipt_writes_nothing(self):
        self.db.execute(
            "INSERT INTO opencode_sessions (session_id,request_key,native_id,model,account,cwd,"
            "root,status,created,updated,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("ses_UNVERIFIED0000000001", "w5-dry-001", "ses_UNVERIFIED0000000001", MODEL,
             str(self.native), str(self.wt), str(self.root), "unverified", 1, 1, "x"))
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest,"
            "creator_pid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("w5-dry-001", MODEL, str(self.native), str(self.wt), str(self.root), 1, 0,
             "creating", 1, 1, "", self.digest(), 0))
        self.db.commit()
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       ("ses_UNVERIFIED0000000001", str(self.wt), "p", MODEL))
        before = self.db.execute("SELECT status FROM opencode_sessions").fetchall()
        # The dry run still refuses the uncertain attempt, and it refuses without
        # having written the promotion the reconciliation would have made.
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.add_sessions(
                self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                request_key="w5-dry-001", dry_run=True)
        self.assertIn("uncertain", str(error.exception))
        after = self.db.execute("SELECT status FROM opencode_sessions").fetchall()
        self.assertEqual(before, after, "the dry run changed a receipt")
        self.assertEqual(self.db.execute("SELECT status FROM opencode_provisioning "
                                         "WHERE request_key='w5-dry-001'").fetchone()[0],
                         "creating")

    def test_a_dry_run_with_a_named_session_writes_nothing(self):
        identifier = "ses_NAMED0000000000001"
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       (identifier, str(self.wt), "p", MODEL))
        result = opencode_lifecycle.add_sessions(
            self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-dry-002", dry_run=True, reconciled_sessions=[identifier])
        self.assertTrue(result["dryRun"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM opencode_sessions").fetchone()[0], 0)
        self.assertEqual(len(paths.config()["opencode"]), 1)


if __name__ == "__main__":
    unittest.main()
