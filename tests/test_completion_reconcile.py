"""Guarded reconciliation of a stale running row whose managed turn completed."""
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import cli, completion, opencode, paths, store  # noqa: E402

TASK = "20261020-fleet-replacement-cutover-w5-01"
TURN = "86f3236e-6327-4862-ace1-b23c0da84896"
SESSION = "ses_eaa694010ffebFoPIycvHTD3uB"
OTHER_SESSION = "ses_a028b8fd0eed4f2c985ed1e0fa"
MODEL = "opencode-go/minimax-m3.1-flash-free"
NOTE = "root reviewed the retained completion receipt; native execution is stopped"


def identity(extra=None, cwd="/tmp/fleet-cutover/worker-5/adea"):
    document = {"taskId": TASK, "sessionId": SESSION, "turn": TURN, "cwd": cwd}
    document.update(extra or {})
    return document


class CompletionReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.live = self.root / "live"
        self.receipts = self.root / "retired"
        self.cwd = self.root / "worker-5" / "adea"
        self.cwd.mkdir(parents=True)
        (self.live / "data").mkdir(parents=True)
        (self.live / "config").mkdir(parents=True)
        (self.live / "config" / "fleet.local.json").write_text(json.dumps({
            "schemaVersion": 2,
            "opencode": [{"sessionId": OTHER_SESSION, "cwd": str(self.cwd),
                          "sessionCwd": str(self.cwd), "model": MODEL,
                          "databasePath": str(self.root / "native.db"),
                          "dispatchEnabled": True}],
        }))
        self.db = store.database(self.live / "data" / "fleet.sqlite")
        self.addCleanup(self.db.close)
        # A retired root keeps its own ledger. It is read to prove that no other
        # root still owns the task, so it must exist and be readable.
        (self.receipts / "data").mkdir(parents=True, exist_ok=True)
        self.retired_db = store.database(self.receipts / "data" / "fleet.sqlite")
        self.addCleanup(self.retired_db.close)
        self.turn_dir = (self.receipts / "data" / "opencode" / SESSION / "turns" / TURN)
        self.turn_dir.mkdir(parents=True)
        self.session_dir = self.turn_dir.parent.parent
        self.lock_path = self.session_dir / "dispatch.lock"
        self.pointer = self.session_dir / "current.json"
        self.pointer.write_text(json.dumps(self.identity({"model": MODEL, "startedAt": 1,
                                                          "wrapperPid": 86549})))
        self.lock_path.write_bytes(b"")
        self.request = self.turn_dir / "request.json"
        self.state = self.turn_dir / "state.json"
        self.result = self.turn_dir / "result.json"
        self.write_receipt()
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (TASK, SESSION, str(self.cwd), json.dumps({"message": "cutover"}),
                         "running", 1))
        self.db.commit()
        for name, value in (("DATA", self.live / "data"), ("DB", self.live / "data" / "fleet.sqlite"),
                            ("CONFIG", self.live / "config" / "fleet.local.json"),
                            ("OPENCODE", self.live / "data" / "opencode"),
                            ("LOGS", self.root / "logs")):
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        roster = patch.object(paths, "opencode_slots", return_value=[
            {"sessionId": OTHER_SESSION, "cwd": str(self.cwd), "model": MODEL}])
        roster.start()
        self.addCleanup(roster.stop)
        lanes = patch.object(paths, "slots", return_value=([], []))
        lanes.start()
        self.addCleanup(lanes.stop)
        dead = patch.object(opencode, "identity_matches", return_value=False)
        dead.start()
        self.addCleanup(dead.stop)
        group = patch.object(opencode, "_group_exists", return_value=False)
        group.start()
        self.addCleanup(group.stop)
        gone = patch.object(os, "kill", side_effect=ProcessLookupError)
        gone.start()
        self.addCleanup(gone.stop)

    def write_receipt(self, state=None, result=None, request=None, pointer=None):
        document = self.identity({
            "model": MODEL, "state": "done", "exitCode": 0, "cleanup": "confirmed",
            "finishedAt": 1792471653.5, "terminalReason": "stop"})
        # The managed state document records no turn id; its own directory binds it.
        document.pop("turn")
        self.state.write_text(json.dumps(state or document))
        self.result.write_text(json.dumps(result or self.identity({
            "state": "done", "exitCode": 0, "cleanup": "confirmed",
            "finishedAt": 1792471653.5, "terminalReason": "stop",
            "managedProcessIdentity": {"pid": 86551, "pgid": 86551, "start": [1792470305, 993459],
                                       "executable": "/usr/bin/opencode"}})))
        self.request.write_text(json.dumps(request or self.identity({"model": MODEL})))
        if pointer is not None:
            self.pointer.write_text(json.dumps(pointer))

    def identity(self, extra=None):
        return identity(extra, str(self.cwd))

    def reconcile(self, **overrides):
        arguments = {"task_id": TASK, "turn": TURN, "session_id": SESSION,
                     "receipt_root": str(self.receipts), "note": NOTE}
        arguments.update(overrides)
        return completion.reconcile_completed(self.db, **arguments)

    def ledger(self):
        return self.db.execute("SELECT id,sid,cwd,body,state FROM tasks ORDER BY id").fetchall()

    def events(self, kind=completion._EVENT_KIND):
        return self.db.execute("SELECT id,body FROM events WHERE kind=?", (kind,)).fetchall()

    def assertLedgerUnchanged(self):
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?", (TASK,)).fetchone(),
                         ("running",))
        self.assertEqual(self.events(), [])

    def assertRetainedLedgerUnchanged(self, expected=0):
        """The retained ledger keeps exactly the rows the test placed there."""
        self.assertEqual(self.retired_db.execute("SELECT count(*) FROM tasks").fetchone()[0],
                         expected)

    def test_verified_completed_row_is_closed_and_audited(self):
        before = self.ledger()
        receipts = {path.name: path.read_bytes() for path in
                    (self.state, self.result, self.request, self.pointer)}
        result = self.reconcile()
        self.assertTrue(result["reconciled"])
        self.assertFalse(result["alreadyRecorded"])
        self.assertEqual(result["previousState"], "running")
        self.assertEqual(result["state"], "finished")
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?", (TASK,)).fetchone(),
                         ("finished",))
        row = self.ledger()
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0][3], before[0][3])
        self.assertEqual(row[0][1], before[0][1])
        self.assertEqual(row[0][2], before[0][2])
        for path in (self.state, self.result, self.request, self.pointer):
            self.assertEqual(path.read_bytes(), receipts[path.name])
        events = self.events()
        self.assertEqual(len(events), 1)
        evidence = json.loads(events[0][1])
        self.assertEqual(evidence["taskId"], TASK)
        self.assertEqual(evidence["turn"], TURN)
        self.assertEqual(evidence["sessionId"], SESSION)
        self.assertEqual(evidence["previousState"], "running")
        self.assertEqual(evidence["state"], "finished")
        self.assertEqual(evidence["exitCode"], 0)
        self.assertEqual(evidence["cleanup"], "confirmed")
        self.assertEqual(evidence["processIdentity"], "gone")
        self.assertEqual(evidence["note"], NOTE)
        self.assertEqual(len(evidence["resultSha256"]), 64)

    def test_reconciliation_is_idempotent_and_never_flips_twice(self):
        first = self.reconcile()
        ledger = self.ledger()
        second = self.reconcile()
        self.assertTrue(second["reconciled"])
        self.assertTrue(second["alreadyRecorded"])
        self.assertEqual(second["record"], first["record"])
        self.assertEqual(self.ledger(), ledger)
        self.assertEqual(len(self.events()), 1)

    def test_changed_receipt_after_the_audit_refuses_a_new_record(self):
        self.reconcile()
        result = json.loads(self.result.read_text())
        result["terminalReason"] = "changed"
        self.result.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "Previously audited reconciliation evidence changed"):
            self.reconcile()
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?", (TASK,)).fetchone(),
                         ("finished",))

    def test_only_the_stale_row_releases_the_same_cwd_exclusivity(self):
        exclusive = ("SELECT 1 FROM tasks WHERE cwd=? AND state IN "
                     "('dispatching','sent','running','uncertain')")
        self.assertIsNotNone(self.db.execute(exclusive, (str(self.cwd),)).fetchone())
        self.reconcile()
        self.assertIsNone(self.db.execute(exclusive, (str(self.cwd),)).fetchone())
        self.assertEqual(self.db.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_queued_task_for_the_same_cwd_is_not_a_conflict(self):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        ("queued-task", OTHER_SESSION, str(self.cwd), json.dumps({"message": "b"}),
                         "queued", 1))
        self.db.commit()
        result = self.reconcile()
        self.assertTrue(result["reconciled"])
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id='queued-task'").fetchone(),
                         ("queued",))

    def test_live_native_process_refuses_with_the_ledger_unchanged(self):
        with patch.object(opencode, "identity_matches", return_value=True):
            with self.assertRaisesRegex(ValueError, "still live"):
                self.reconcile()
        self.assertLedgerUnchanged()

    def test_live_native_process_group_refuses(self):
        with patch.object(opencode, "_group_exists", return_value=True):
            with self.assertRaisesRegex(ValueError, "group is still present"):
                self.reconcile()
        self.assertLedgerUnchanged()

    def test_live_wrapper_process_refuses(self):
        with patch.object(os, "kill", return_value=None):
            with self.assertRaisesRegex(ValueError, "wrapper PID is still present"):
                self.reconcile()
        self.assertLedgerUnchanged()

    def test_uncertain_or_failed_outcome_refuses(self):
        cases = [
            ({"state": "error", "exitCode": 1, "cleanup": "confirmed"}, "does not prove a completed"),
            ({"state": "done", "exitCode": 1, "cleanup": "confirmed"}, "successful exit"),
            ({"state": "done", "exitCode": 0, "cleanup": "unconfirmed"}, "confirm cleanup"),
            ({"state": "done", "exitCode": 0, "cleanup": "confirmed", "finishedAt": None},
             "no completion timestamp"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                document = {"model": MODEL, "state": "done", "exitCode": 0, "cleanup": "confirmed",
                            "finishedAt": 1792471653.5}
                document.update(overrides)
                self.write_receipt(state=self.identity(document))
                with self.assertRaisesRegex(ValueError, message):
                    self.reconcile()
                self.assertLedgerUnchanged()

    def test_mismatched_task_turn_session_or_cwd_refuses(self):
        cases = [
            ({"request": self.identity({"model": MODEL, "taskId": "other-task"})}, "does not identify"),
            ({"state": self.identity({"model": MODEL, "turn": "other-turn"})}, "does not identify"),
            ({"result": self.identity({"sessionId": "ses_other"})}, "does not identify"),
            ({"pointer": self.identity({"model": MODEL, "cwd": "/tmp/elsewhere"})}, "working directory"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=sorted(overrides)):
                self.write_receipt(**overrides)
                with self.assertRaisesRegex(ValueError, message):
                    self.reconcile()
                self.assertLedgerUnchanged()
        self.write_receipt()

    def test_missing_or_unreadable_receipt_refuses(self):
        for name in ("state.json", "result.json", "request.json"):
            with self.subTest(name=name):
                path = self.turn_dir / name
                body = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(ValueError, "is missing"):
                    self.reconcile()
                path.write_bytes(body)
        self.pointer.unlink()
        with self.assertRaisesRegex(ValueError, "is missing"):
            self.reconcile()
        self.pointer.write_text("{not json")
        with self.assertRaisesRegex(ValueError, "is unreadable"):
            self.reconcile()
        self.assertLedgerUnchanged()

    def test_competing_task_for_the_same_cwd_refuses(self):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        ("competing", OTHER_SESSION, str(self.cwd), json.dumps({"message": "b"}),
                         "running", 1))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "owns this working directory"):
            self.reconcile()
        self.assertLedgerUnchanged()

    def test_another_non_terminal_task_for_the_same_worker_refuses(self):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        ("other", SESSION, str(self.root / "other"), json.dumps({"message": "x"}),
                         "uncertain", 1))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "another non-terminal task"):
            self.reconcile()
        self.assertLedgerUnchanged()

    def test_worker_still_managed_by_this_fleet_refuses(self):
        with patch.object(paths, "opencode_slots", return_value=[
                {"sessionId": SESSION, "cwd": str(self.cwd), "model": MODEL}]):
            with self.assertRaisesRegex(ValueError, "still managed by this fleet"):
                self.reconcile()
        self.assertLedgerUnchanged()

    def test_uncertain_ledger_row_is_never_reconciled(self):
        self.db.execute("UPDATE tasks SET state='uncertain' WHERE id=?", (TASK,))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "resolve it with `fleet resolve`"):
            self.reconcile()
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?", (TASK,)).fetchone(),
                         ("uncertain",))
        self.assertEqual(self.events(), [])

    def test_terminal_ledger_row_is_never_reconciled(self):
        self.db.execute("UPDATE tasks SET state='finished' WHERE id=?", (TASK,))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "only a stale running or sent row"):
            self.reconcile()
        self.assertEqual(self.events(), [])

    def test_unreadable_or_malformed_roster_configuration_refuses(self):
        missing = self.root / "no-such-config.json"
        malformed = self.root / "malformed-config.json"
        malformed.write_text("{not json")
        invalid = self.root / "invalid-config.json"
        invalid.write_text(json.dumps(["not", "a", "roster"]))
        for path, message in ((missing, "missing or unreadable"),
                              (malformed, "missing or unreadable"),
                              (invalid, "is invalid")):
            with self.subTest(path=path.name):
                with patch.object(paths, "CONFIG", path):
                    with self.assertRaisesRegex(ValueError, message):
                        self.reconcile()
                self.assertLedgerUnchanged()

    def test_missing_or_unreadable_retired_root_ledger_refuses(self):
        retired = self.receipts / "data" / "fleet.sqlite"
        retired.unlink()
        with self.assertRaisesRegex(ValueError, "ledger is missing"):
            self.reconcile()
        self.assertLedgerUnchanged()
        # A corrupt ledger: the write-ahead pages are removed with the open
        # connection, so the reader really sees an unreadable database.
        self.retired_db.close()
        for suffix in ("-wal", "-shm"):
            Path(str(retired) + suffix).unlink(missing_ok=True)
        retired.write_bytes(b"this is not a database")
        with self.assertRaisesRegex(ValueError, "ledger is unreadable"):
            self.reconcile()
        self.assertLedgerUnchanged()
        retired.unlink()
        other = store.database(retired)
        self.addCleanup(other.close)
        other.execute("DROP TABLE tasks")
        other.commit()
        with self.assertRaisesRegex(ValueError, "ledger is unreadable"):
            self.reconcile()
        self.assertLedgerUnchanged()

    def test_another_root_still_owning_the_task_refuses(self):
        self.retired_db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                                 (TASK, SESSION, str(self.cwd), "{}", "running", 1))
        self.retired_db.commit()
        with self.assertRaisesRegex(ValueError, "Another Fleet root still owns the task"):
            self.reconcile()
        self.assertLedgerUnchanged()
        self.assertRetainedLedgerUnchanged(expected=1)

    def test_another_root_task_under_the_same_session_refuses(self):
        self.retired_db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                                ("other-retained", SESSION, str(self.root / "elsewhere"), "{}",
                                 "running", 1))
        self.retired_db.commit()
        with self.assertRaisesRegex(ValueError, "Another Fleet root still owns the task"):
            self.reconcile()
        self.assertLedgerUnchanged()
        self.assertRetainedLedgerUnchanged(expected=1)

    def test_another_root_task_under_the_same_cwd_refuses(self):
        self.retired_db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                                ("other-retained", OTHER_SESSION, str(self.cwd), "{}",
                                 "uncertain", 1))
        self.retired_db.commit()
        with self.assertRaisesRegex(ValueError, "Another Fleet root still owns the task"):
            self.reconcile()
        self.assertLedgerUnchanged()
        self.assertRetainedLedgerUnchanged(expected=1)

    def test_a_retained_root_claim_between_validation_and_commit_refuses(self):
        with patch.object(completion, "_retained_ownership_fence",
                          side_effect=ValueError("Another Fleet root still owns the task")):
            with self.assertRaisesRegex(ValueError, "Another Fleet root still owns the task"):
                self.reconcile()
        self.assertLedgerUnchanged()

    def test_a_retained_ledger_claim_cannot_slip_through_the_commit_window(self):
        """Two real WAL ledgers: a cross-root claim in the commit window is fenced."""
        self.assertEqual(self.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.retired_db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        retained = self.receipts / "data" / "fleet.sqlite"
        outcome = {}

        def claim():
            connection = sqlite3.connect(retained, timeout=0.5)
            try:
                connection.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                                   ("late-claim", SESSION, str(self.cwd), "{}", "running", 1))
                connection.commit()
                outcome["claimed"] = True
            except sqlite3.Error as error:
                outcome["claimed"] = False
                outcome["error"] = str(error)
            finally:
                connection.close()

        original = completion._assert_no_conflict
        calls = []

        def hook(rows, task_id, session_id, resolved_cwd):
            calls.append(rows)
            if len(calls) == 2:
                # The last retained-ledger ownership query has already run and
                # the current-ledger update has not happened yet: this is the
                # window a cross-root claim would slip through.
                worker = threading.Thread(target=claim)
                worker.start()
                worker.join(timeout=10)
                outcome["finished"] = not worker.is_alive()
            return original(rows, task_id, session_id, resolved_cwd)

        with patch.object(completion, "_assert_no_conflict", side_effect=hook):
            self.reconcile()
        self.assertEqual(len(calls), 2)
        self.assertTrue(outcome.get("finished"))
        self.assertFalse(outcome.get("claimed"))
        self.assertIn("locked", str(outcome.get("error")))
        # The fenced claim never landed in the retained ledger.
        self.assertRetainedLedgerUnchanged()
        # The same claim is accepted once the fence is released, so the refusal
        # was the fence and not a broken ledger.
        connection = sqlite3.connect(retained, timeout=2)
        connection.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                           ("late-claim", SESSION, str(self.cwd), "{}", "running", 1))
        connection.commit()
        connection.close()
        self.assertEqual(self.retired_db.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_same_physical_ledger_reuses_transaction_and_ignores_only_current_row(self):
        # The receipt root may be the current Fleet root. The target's own
        # running row is valid, and an unrelated unrostered row is preserved.
        alias_root = self.root / "same-ledger-alias"
        (alias_root / "data").mkdir(parents=True)
        (alias_root / "data" / "fleet.sqlite").symlink_to(
            self.live / "data" / "fleet.sqlite")
        shutil.copytree(self.session_dir, alias_root / "data" / "opencode" / SESSION)
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        ("unrelated", "ses_unrostered", str(self.root / "elsewhere"), "{}",
                         "running", 1))
        self.db.commit()
        result = self.reconcile(receipt_root=str(alias_root))
        self.assertTrue(result["reconciled"])
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?", (TASK,))
                         .fetchone(), ("finished",))
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id='unrelated'")
                         .fetchone(), ("running",))

    def test_missing_process_identity_refuses(self):
        result = self.identity({
            "state": "done", "exitCode": 0, "cleanup": "confirmed",
            "finishedAt": 1792471653.5, "terminalReason": "stop"})
        self.result.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "records no process identity"):
            self.reconcile()
        self.assertLedgerUnchanged()

    def test_missing_wrapper_pid_refuses(self):
        pointer = self.identity({"model": MODEL, "startedAt": 1})
        self.pointer.write_text(json.dumps(pointer))
        with self.assertRaisesRegex(ValueError, "wrapper PID is missing"):
            self.reconcile()
        self.assertLedgerUnchanged()

    def test_contradictory_null_identity_with_a_completed_turn_refuses(self):
        result = self.identity({
            "state": "done", "exitCode": 0, "cleanup": "confirmed",
            "finishedAt": 1792471653.5, "terminalReason": "stop",
            "managedProcessIdentity": None})
        self.result.write_text(json.dumps(result))
        self.pointer.write_text(json.dumps(self.identity({"model": MODEL, "startedAt": 1})))
        with self.assertRaisesRegex(ValueError, "no native process identity for a completed turn"):
            self.reconcile()
        self.assertLedgerUnchanged()

    def test_a_partial_native_identity_refuses(self):
        for identity in ({"pid": 86551}, {"pid": 86551, "pgid": 86551},
                         {"pid": 86551, "pgid": 86551, "start": [1]},
                         {"pid": 86551, "pgid": 86551, "start": [1], "executable": ""}):
            with self.subTest(identity=sorted(identity)):
                result = self.identity({
                    "state": "done", "exitCode": 0, "cleanup": "confirmed",
                    "finishedAt": 1792471653.5, "terminalReason": "stop",
                    "managedProcessIdentity": identity})
                self.result.write_text(json.dumps(result))
                with self.assertRaisesRegex(ValueError, "invalid native process identity"):
                    self.reconcile()
                self.assertLedgerUnchanged()
        self.write_receipt()

    def test_a_launch_holding_the_session_lock_refuses(self):
        handle = self.lock_path.open("rb")
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaisesRegex(ValueError, "managed turn is active"):
            self.reconcile()
        self.assertLedgerUnchanged()

    def test_a_task_claiming_the_cwd_between_validation_and_commit_refuses(self):
        competing = [("competing", str(self.cwd), OTHER_SESSION)]
        with patch.object(completion, "_conflict_rows",
                          side_effect=[[], competing]) as rows:
            with self.assertRaisesRegex(ValueError, "owns this working directory"):
                self.reconcile()
            self.assertEqual(rows.call_count, 2)
        self.assertLedgerUnchanged()

    def test_the_session_lock_is_held_through_the_commit(self):
        observed = {}

        def audited(db, kind, **body):
            handle = self.lock_path.open("rb")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observed["held"] = True
            else:
                observed["held"] = False
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
            return store.emit(db, kind, **body)

        with patch.object(completion, "emit", side_effect=audited):
            self.reconcile()
        self.assertTrue(observed.get("held"))
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?", (TASK,)).fetchone(),
                         ("finished",))

    def test_invalid_arguments_are_refused(self):
        with self.assertRaisesRegex(ValueError, "operator note"):
            self.reconcile(note="  ")
        with self.assertRaisesRegex(ValueError, "turn is invalid"):
            self.reconcile(turn="../escape")
        with self.assertRaisesRegex(ValueError, "session id is invalid"):
            self.reconcile(session_id="not-a-session")
        with self.assertRaisesRegex(ValueError, "Task not found"):
            self.reconcile(task_id="missing-task")
        with self.assertRaisesRegex(ValueError, "does not belong to the requested worker"):
            self.reconcile(session_id=OTHER_SESSION)
        with self.assertRaisesRegex(ValueError, "Receipt root must be an absolute path"):
            self.reconcile(receipt_root="relative/root")
        with self.assertRaisesRegex(ValueError, "Receipt root does not exist"):
            self.reconcile(receipt_root=str(self.root / "absent"))
        self.assertEqual(self.events(), [])

    def test_holds_pause_and_dispatch_state_are_untouched(self):
        self.db.execute("INSERT INTO task_holds VALUES (?,?,?,?,?)",
                        ("held-task", "quota hold", "held", 1, 1))
        self.db.commit()
        holds = self.db.execute("SELECT * FROM task_holds").fetchall()
        pause = self.live / "data" / "dispatch.paused"
        pause.write_text("paused")
        self.reconcile()
        self.assertEqual(self.db.execute("SELECT * FROM task_holds").fetchall(), holds)
        self.assertTrue(pause.exists())
        self.assertEqual(pause.read_text(), "paused")
        self.assertEqual(self.db.execute("SELECT count(*) FROM task_holds").fetchone()[0], 1)

    def test_command_line_refuses_with_json_error_and_exits_non_zero(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["reconcile-completed", "--task", TASK, "--turn", TURN,
                          "--worker", SESSION, "--receipt-root", str(self.root / "absent"),
                          "--note", "no receipt here"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("error", buffer.getvalue())
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?", (TASK,)).fetchone(),
                         ("running",))

    def test_command_line_reconciles_the_verified_row(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli.main(["reconcile-completed", "--task", TASK, "--turn", TURN,
                      "--worker", SESSION, "--receipt-root", str(self.receipts), "--note", NOTE])
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["reconciled"])
        self.assertEqual(payload["previousState"], "running")
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id=?", (TASK,)).fetchone(),
                         ("finished",))


if __name__ == "__main__":
    unittest.main()
