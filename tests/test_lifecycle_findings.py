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
        root = Path(self.tmp.name).resolve()
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
        # The dry run reports the request's state and writes nothing: not the
        # promotion the reconciliation would have made.
        result = opencode_lifecycle.add_sessions(
            self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-dry-001", dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["requestStatus"], "creating")
        after = self.db.execute("SELECT status FROM opencode_sessions").fetchall()
        self.assertEqual(before, after, "the dry run changed a receipt")
        self.assertEqual(self.db.execute("SELECT status FROM opencode_provisioning "
                                         "WHERE request_key='w5-dry-001'").fetchone()[0],
                         "creating")

    def test_a_dry_run_does_not_qualify_the_native_schema(self):
        # The documented contract: a dry run writes nothing, and it does not
        # consult the installed CLI's own schema. The schema is qualified on
        # the creation path, before the request is sent, so the dry run's
        # report says nothing about whether the call is proved provider-free.
        def refuse_probe(executable, timeout=None):
            raise AssertionError("the dry run consulted the installed CLI's own schema")

        with patch.object(opencode_lifecycle, "verify_native_api_support", refuse_probe):
            result = opencode_lifecycle.add_sessions(
                self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                request_key="w5-dry-003", dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["created"], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM opencode_sessions").fetchone()[0], 0)

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


class QueueEvidenceTests(FindingSandbox):
    """Finding 1: a missing queue is no queue; an invalid one is not evidence."""

    def test_a_missing_queue_file_is_not_a_refusal(self):
        self.assertFalse(paths.QUEUE.exists())
        result = opencode_lifecycle.remove_session(self.db, self.existing, note="remove")
        self.assertTrue(result["removed"])

    def test_an_empty_queue_file_refuses_the_removal(self):
        paths.QUEUE.write_text("")
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing, note="remove")
        self.assertIn("empty", str(error.exception))
        self.assertEqual(len(paths.config()["opencode"]), 1)

    def test_a_whitespace_queue_file_refuses_the_removal(self):
        paths.QUEUE.write_text("   \n\t ")
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing, note="remove")
        self.assertIn("empty", str(error.exception))
        self.assertEqual(len(paths.config()["opencode"]), 1)

    def test_a_truncated_queue_file_refuses_the_removal(self):
        paths.QUEUE.write_text('{"author": "root", "tasks": [{"id": "t-1"')
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing, note="remove")
        self.assertIn("malformed", str(error.exception))

    def test_a_queue_without_a_tasks_list_refuses_the_removal(self):
        paths.QUEUE.write_text(json.dumps({"author": "root"}))
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing, note="remove")
        self.assertIn("malformed", str(error.exception))

    def test_a_queue_that_is_not_an_object_refuses_the_removal(self):
        paths.QUEUE.write_text(json.dumps([{"id": "t-1"}]))
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing, note="remove")
        self.assertIn("malformed", str(error.exception))

    def test_a_queue_without_the_root_author_refuses_the_removal(self):
        paths.QUEUE.write_text(json.dumps({"author": "someone", "tasks": []}))
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing, note="remove")
        self.assertIn("root author", str(error.exception))


class SnapshotDigestTests(FindingSandbox):
    """Finding 2: the selection compares the configuration with its own snapshot."""

    def test_an_out_of_band_change_at_the_boundary_is_refused_and_kept(self):
        original = model_selection.plan

        def plan_with_an_out_of_band_change(cfg, model, session_id=None, db=None):
            planned = original(cfg, model, session_id, db)
            # Another writer changes the configuration after the snapshot was
            # taken and before the write.
            changed = json.loads(paths.CONFIG.read_text())
            changed["maxConcurrent"] = 9
            paths.CONFIG.write_text(json.dumps(changed))
            return planned

        with patch.object(model_selection, "plan", side_effect=plan_with_an_out_of_band_change):
            with self.assertRaises(ValueError) as error:
                model_selection.apply(None, "opencode/minimax-m3.1-flash-free", self.existing,
                                      dry_run=False, db=self.db)
        self.assertIn("changed while", str(error.exception))
        # The out-of-band change is still there: it was never overwritten.
        self.assertEqual(paths.config()["maxConcurrent"], 9)
        self.assertEqual(paths.config()["opencode"][0]["model"], MODEL)

    def test_a_selection_without_a_change_writes_nothing(self):
        result = model_selection.apply(None, MODEL, self.existing, dry_run=False, db=self.db)
        self.assertFalse(result["applied"])
        self.assertEqual(paths.config()["opencode"][0]["model"], MODEL)


class PartialReconciliationTests(FindingSandbox):
    """Finding 3: a partial reconciliation leaves a native effect unaccounted."""

    def _uncertain_row(self, key, count):
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest,"
            "creator_pid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, MODEL, str(self.native), str(self.wt), str(self.root), count, 0, "uncertain",
             1, 1, "", self.digest(count), 0))
        self.db.commit()

    def _receipt(self, key, session_id):
        self.db.execute(
            "INSERT INTO opencode_sessions (session_id,request_key,native_id,model,account,cwd,"
            "root,status,created,updated,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, key, session_id, MODEL, str(self.native), str(self.wt), str(self.root),
             "created", 1, 1, ""))
        self.db.commit()

    def test_a_partial_reconciliation_refuses(self):
        # Three were requested. One was receipted and the operator names a
        # second; the third native effect is unaccounted, so the request refuses
        # instead of succeeding with a subset or creating another.
        self._uncertain_row("w5-partial-001", 3)
        self._receipt("w5-partial-001", "ses_ONE000000000000000001")
        named = "ses_TWO000000000000000001"
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       (named, str(self.wt), "p", MODEL))
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.add_sessions(
                self.db, count=3, model=MODEL, cwd=str(self.wt), root=str(self.root),
                request_key="w5-partial-001", reconciled_sessions=[named])
        self.assertIn("uncertain", str(error.exception))
        self.assertEqual(len(paths.config()["opencode"]), 1)

    def test_a_complete_reconciliation_continues(self):
        # Every requested effect is named, so the continuation is exact.
        self._uncertain_row("w5-partial-002", 2)
        first = "ses_ONE000000000000000002"
        second = "ses_TWO000000000000000002"
        with sqlite3.connect(self.native) as db:
            for identifier in (first, second):
                db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                           (identifier, str(self.wt), "p", MODEL))
        result = opencode_lifecycle.add_sessions(
            self.db, count=2, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-partial-002", reconciled_sessions=[first, second])
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(paths.config()["opencode"]), 3)


class StaleCallerTests(FindingSandbox):
    """Finding 4: the initial intent rechecks the bound payload and the terminal state."""

    def _registered_row(self, key, count):
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest,"
            "creator_pid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, MODEL, str(self.native), str(self.wt), str(self.root), count, 0, "registered",
             1, 1, "completed by another caller", self.digest(count), 0))
        self.db.commit()

    def test_a_stale_caller_returns_idempotently_and_keeps_the_registered_row(self):
        # Another caller completed this request while this one was preparing.
        self._registered_row("w5-stale-001", 1)
        result = opencode_lifecycle.add_sessions(
            self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-stale-001")
        self.assertTrue(result["idempotent"])
        row = opencode_lifecycle._provisioning_row(self.db, "w5-stale-001")
        self.assertEqual(row[7], "registered")
        self.assertEqual(row[10], "completed by another caller")
        self.assertEqual(len(paths.config()["opencode"]), 1)

    def test_a_stale_caller_with_a_different_payload_is_refused(self):
        self._registered_row("w5-stale-002", 1)
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.add_sessions(
                self.db, count=2, model=MODEL, cwd=str(self.wt), root=str(self.root),
                request_key="w5-stale-002")
        self.assertIn("different request", str(error.exception))
        row = opencode_lifecycle._provisioning_row(self.db, "w5-stale-002")
        self.assertEqual(row[7], "registered")
        self.assertEqual(len(paths.config()["opencode"]), 1)

    def test_a_two_caller_barrier_leaves_one_registered_row(self):
        # Two callers race for the same key. The first one completes the
        # request; the second one arrives afterwards and must return
        # idempotently without overwriting the completed row or creating a
        # second session.
        finished = threading.Event()
        first = {}

        def caller_one():
            connection = store.database(paths.DB)
            try:
                first["result"] = opencode_lifecycle.add_sessions(
                    connection, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                    request_key="w5-stale-003")
            finally:
                connection.close()
                finished.set()

        worker = threading.Thread(target=caller_one)
        worker.start()
        finished.wait(60)
        worker.join(30)
        self.assertEqual(first["result"]["created"], 1)

        connection = store.database(paths.DB)
        try:
            second = opencode_lifecycle.add_sessions(
                connection, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                request_key="w5-stale-003")
        finally:
            connection.close()
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["created"], 0)
        row = opencode_lifecycle._provisioning_row(self.db, "w5-stale-003")
        self.assertEqual(row[7], "registered")
        # One native session, one roster entry: the second caller added nothing.
        self.assertEqual(len(paths.config()["opencode"]), 2)


class StaleSnapshotBarrierTests(FindingSandbox):
    """A caller that planned from a stale snapshot creates nothing and overwrites nothing."""

    def creations(self):
        if not self.fake_log.exists():
            return []
        return [json.loads(line) for line in self.fake_log.read_text().splitlines()
                if line.strip() and "POST" in line and "/api/session" in line]

    def test_a_stale_caller_creates_nothing_and_never_overwrites(self):
        # B takes its snapshot and pauses. A runs the request, creates its
        # native session and pauses before the roster registration. B resumes
        # with the snapshot it took before any of that.
        b_snapshot = threading.Event()
        release_b = threading.Event()
        a_effect = threading.Event()
        release_a = threading.Event()
        real_intent = opencode_lifecycle._locked_intent
        real_record = opencode_lifecycle._record_provisioning
        outcome = {"b": None, "a": None}

        def intent(db, key, request, count, digest, reconciled, note):
            if threading.current_thread() is b_thread:
                b_snapshot.set()
                release_b.wait(60)
            return real_intent(db, key, request, count, digest, reconciled, note)

        def record(db, key, request, status, detail="", count=None, payload_digest="",
                   creator_pid=0, keep_registered=False):
            if (threading.current_thread() is a_thread and status == "creating"
                    and detail.startswith("{")):
                # A has created its native session and recorded the ids; it
                # pauses before the roster registration.
                a_effect.set()
                release_a.wait(60)
            return real_record(db, key, request, status, detail, count, payload_digest,
                               creator_pid, keep_registered)

        def run_b():
            connection = store.database(paths.DB)
            try:
                outcome["b"] = opencode_lifecycle.add_sessions(
                    connection, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                    request_key="w5-barrier-001")
            except Exception as error:  # noqa: BLE001 - the refusal is the result
                outcome["b"] = {"error": str(error)}
            finally:
                connection.close()

        def run_a():
            connection = store.database(paths.DB)
            try:
                outcome["a"] = opencode_lifecycle.add_sessions(
                    connection, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                    request_key="w5-barrier-001")
            except Exception as error:  # noqa: BLE001 - the refusal is the result
                outcome["a"] = {"error": str(error)}
            finally:
                connection.close()

        b_thread = threading.Thread(target=run_b)
        a_thread = threading.Thread(target=run_a)
        with patch.object(opencode_lifecycle, "_locked_intent", side_effect=intent), \
             patch.object(opencode_lifecycle, "_record_provisioning", side_effect=record):
            b_thread.start()
            self.assertTrue(b_snapshot.wait(60), "B never took its snapshot")
            a_thread.start()
            self.assertTrue(a_effect.wait(120), "A never reached its native effect")
            # B resumes with the snapshot it took before A's effect.
            release_b.set()
            b_thread.join(60)
            self.assertIn("error", outcome["b"], "B did not refuse the taken-over request")
            self.assertIn("in progress", outcome["b"]["error"])
            # Exactly one native session was created: B created nothing.
            self.assertEqual(len(self.creations()), 1)
            # B wrote no roster entry and left the request's row alone.
            self.assertEqual(len(paths.config()["opencode"]), 1)
            row = opencode_lifecycle._provisioning_row(self.db, "w5-barrier-001")
            self.assertEqual(row[7], "creating")
            # A finishes its registration.
            release_a.set()
            a_thread.join(120)
        self.assertNotIn("error", outcome["a"])
        self.assertEqual(len(self.creations()), 1)
        self.assertEqual(len(paths.config()["opencode"]), 2)
        row = opencode_lifecycle._provisioning_row(self.db, "w5-barrier-001")
        self.assertEqual(row[7], "registered")
        receipts = opencode_lifecycle._session_receipts(self.db, "w5-barrier-001")
        self.assertEqual([receipt["status"] for receipt in receipts], ["registered"])


if __name__ == "__main__":
    unittest.main()
