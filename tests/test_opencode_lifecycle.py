"""On-demand OpenCode session lifecycle: synthetic inventories and injected failures.

Every test here runs against a temporary fleet root, a synthetic native session
inventory and a fake OpenCode CLI. No real native database is written, no
provider is contacted and no live fleet is touched.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import (dispatch, heavy, model_selection, opencode,  # noqa: E402
                 opencode_lifecycle, ops, paths, store)

MODEL = "opencode-go/minimax-m3.1-flash-free"
OTHER_APPROVED = "opencode/minimax-m3.1-flash-free"

FAKE_CLI = '''#!/usr/bin/env python3
"""A fake OpenCode CLI: it records the request and writes the native session row."""
import json
import os
import sqlite3
import sys
import time

CONTROL = {control!r}
DATABASE = {database!r}
LOG = {log!r}

control = json.load(open(CONTROL))
argv = sys.argv[1:]
data = None
for index, value in enumerate(argv):
    if value == "--data":
        data = json.loads(argv[index + 1])
calls = 0
if os.path.exists(LOG):
    with open(LOG) as stream:
        calls = sum(1 for line in stream if line.strip())
with open(LOG, "a") as stream:
    stream.write(json.dumps({{"argv": argv, "data": data,
                             "config": os.environ.get("OPENCODE_CONFIG_CONTENT"),
                             "db": os.environ.get("OPENCODE_DB"),
                             "home": os.environ.get("HOME")}}) + "\\n")
mode = control.get("mode", "ok")
if mode == "hang":
    time.sleep(600)
if mode == "fail" or (mode == "okThenFail" and calls >= control.get("failAfter", 1)):
    sys.stderr.write("fake opencode refused the request\\n")
    raise SystemExit(3)
if mode == "garbage":
    print("not json at all")
    raise SystemExit(0)
session_id = control["sessionId"]
if control.get("unique"):
    session_id = f"{{session_id}}-{{calls}}"
directory = (data or {{}}).get("location", {{}}).get("directory")
if data:
    with sqlite3.connect(DATABASE) as database:
        database.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                         (session_id, directory, "project-test", data.get("model")))
print(json.dumps({{"data": {{"id": session_id, "directory": directory,
                          "model": (data or {{}}).get("model")}}}}))
'''


class Sandbox(unittest.TestCase):
    """A temporary fleet root with a synthetic native session inventory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        self.wt = root / "wt" / "repo"
        self.wt.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.wt)], check=True)
        self.native = root / "native" / "opencode.db"
        self.native.parent.mkdir()
        with sqlite3.connect(self.native) as db:
            db.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, project_id TEXT, "
                       "model TEXT)")
        self.existing = "ses_EXISTING000000000001"
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT INTO session_v2 VALUES (?,?,?,?)",
                       (self.existing, str(self.wt), "project-test", MODEL))
        self.control = root / "control.json"
        self.log = root / "fake-cli.log"
        self.write_control({"mode": "ok", "sessionId": "ses_NEW0000000000000001"})
        self.fake = root / "fake-opencode"
        self.fake.write_text(FAKE_CLI.format(control=str(self.control), database=str(self.native),
                                             log=str(self.log)))
        self.fake.chmod(0o755)
        self.cfg = {
            "worktreeRoot": str(root / "wt"),
            "executables": {"opencode": str(self.fake)},
            "maxConcurrent": 4,
            "pi": [], "claude": [],
            "opencode": [{"sessionId": self.existing, "cwd": str(self.wt),
                          "sessionCwd": str(self.wt), "databasePath": str(self.native),
                          "model": MODEL, "provider": "opencode", "dispatchEnabled": True}],
        }
        (root / "config").mkdir()
        (root / "config" / "fleet.local.json").write_text(json.dumps(self.cfg))
        (root / "data").mkdir()
        values = {"ROOT": root, "CONFIG": root / "config" / "fleet.local.json",
                  "DATA": root / "data", "LOGS": root / "logs", "DB": root / "data" / "fleet.sqlite",
                  "STATUS": root / "data" / "status.json", "MANAGER_LOCK": root / "data" / "manager.lock",
                  "OPENCODE": root / "data" / "opencode", "BIN": root / "bin" / "fleet",
                  "QUEUE": root / "data" / "queue.json"}
        for name, value in values.items():
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.db = store.database(paths.DB)
        self.addCleanup(self.db.close)

    def write_control(self, control):
        self.control.write_text(json.dumps(control))

    def write_config(self, cfg):
        paths.CONFIG.write_text(json.dumps(cfg))

    def roster(self):
        return paths.config().get("opencode", [])

    def requests(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def add(self, count=1, model=MODEL, cwd=None, root=None, **kwargs):
        return opencode_lifecycle.add_sessions(
            self.db, count=count, model=model, cwd=cwd or str(self.wt),
            root=str(root or self.root), **kwargs)

    def task(self, task_id, sid, state):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (task_id, sid, str(self.wt), json.dumps({"message": "m"}), state, 1))
        self.db.commit()


class NativeRequestTests(Sandbox):
    def test_the_native_request_carries_the_exact_model_and_directory(self):
        self.add(count=1)
        requests = self.requests()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["argv"][:4], ["api", "--standalone", "POST", "/api/session"])
        self.assertEqual(requests[0]["data"]["model"], MODEL)
        self.assertEqual(requests[0]["data"]["location"]["directory"], str(self.wt))
        # The isolated runtime pins the model and its provider and reaches no other.
        config = json.loads(requests[0]["config"])
        self.assertEqual(config["model"], MODEL)
        self.assertEqual(config["small_model"], MODEL)
        self.assertEqual(config["enabled_providers"], ["opencode-go"])
        self.assertEqual(requests[0]["db"], str(self.native))
        self.assertNotEqual(requests[0]["home"], str(Path.home()))

    def test_the_created_session_is_read_back_from_the_native_inventory(self):
        result = self.add(count=1)
        self.assertEqual(result["created"], 1)
        identifier = result["sessions"][0]["sessionId"]
        self.assertTrue(identifier.startswith("ses_"))
        with sqlite3.connect(self.native) as db:
            row = db.execute("SELECT id,directory,model FROM session_v2 WHERE id=?",
                             (identifier,)).fetchone()
        self.assertEqual(row[0], identifier)
        self.assertEqual(row[1], str(self.wt))
        self.assertEqual(row[2], MODEL)
        self.assertEqual([entry["sessionId"] for entry in self.roster()],
                         [self.existing, identifier])

    def test_an_existing_session_is_never_recreated_or_changed(self):
        before = dict(self.roster()[0])
        self.add(count=1)
        self.assertEqual(self.roster()[0], before)
        self.assertEqual(len(self.requests()), 1)
        with sqlite3.connect(self.native) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM session_v2 WHERE id=?", (self.existing,)).fetchone()[0],
                1)


class ModelPinTests(Sandbox):
    def test_a_model_outside_the_approved_set_is_refused(self):
        with self.assertRaises(ValueError):
            self.add(count=1, model="opencode/some-paid-model")
        self.assertEqual(self.requests(), [])
        self.assertEqual(len(self.roster()), 1)

    def test_a_model_that_is_not_the_account_model_is_refused(self):
        with self.assertRaises(ValueError):
            self.add(count=1, model=OTHER_APPROVED)
        self.assertEqual(self.requests(), [])

    def test_a_model_the_account_uses_is_accepted(self):
        self.cfg["opencode"][0]["model"] = OTHER_APPROVED
        self.write_config(self.cfg)
        result = self.add(count=1, model=OTHER_APPROVED)
        self.assertEqual(result["created"], 1)
        self.assertEqual(self.requests()[0]["data"]["model"], OTHER_APPROVED)


class IdempotencyTests(Sandbox):
    def test_the_same_request_key_never_adds_twice(self):
        first = self.add(count=1, request_key="w5-scale-001")
        self.assertEqual(first["created"], 1)
        second = self.add(count=1, request_key="w5-scale-001")
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["created"], 0)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.roster()), 2)

    def test_the_same_request_without_a_key_is_idempotent_by_state(self):
        self.add(count=1)
        again = self.add(count=1)
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.roster()), 2)

    def test_a_retry_after_a_failure_registers_the_created_session(self):
        # The first session is created, the second fails: the roster is untouched
        # and the created native session is kept and recorded.
        self.write_control({"mode": "okThenFail", "failAfter": 1,
                            "sessionId": "ses_NEW0000000000000001"})
        failed = self.add(count=2, request_key="w5-scale-002")
        self.assertFalse(failed["rosterChanged"])
        self.assertEqual(len(self.roster()), 1)
        receipts = opencode_lifecycle._session_receipts(self.db, "w5-scale-002")
        self.assertEqual([receipt["status"] for receipt in receipts], ["created"])
        self.assertEqual(failed["createdNotRegistered"], ["ses_NEW0000000000000001"])
        with sqlite3.connect(self.native) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM session_v2 WHERE id=?",
                           ("ses_NEW0000000000000001",)).fetchone()[0], 1)
        # The retry registers the session that already exists and creates nothing.
        self.write_control({"mode": "ok", "sessionId": "ses_NEW0000000000000002"})
        resumed = self.add(count=2, request_key="w5-scale-002")
        # The session created before the failure is registered, and only the
        # missing one is created: two sessions, one new native session.
        self.assertEqual(resumed["registered"], 2)
        self.assertEqual(resumed["created"], 1)
        self.assertEqual([entry["sessionId"] for entry in self.roster()],
                         [self.existing, "ses_NEW0000000000000001", "ses_NEW0000000000000002"])


class PartialFailureTests(Sandbox):
    def test_a_partial_failure_leaves_the_roster_untouched(self):
        self.write_control({"mode": "okThenFail", "failAfter": 1,
                            "sessionId": "ses_NEW0000000000000001"})
        result = self.add(count=2, request_key="w5-scale-003")
        self.assertFalse(result["rosterChanged"])
        self.assertEqual(len(self.roster()), 1)
        self.assertTrue(result["failed"])
        self.assertEqual(result["createdNotRegistered"], ["ses_NEW0000000000000001"])
        kinds = [row[0] for row in self.db.execute("SELECT kind FROM events")]
        self.assertIn("needs_attention", kinds)

    def test_a_failed_request_leaves_no_half_written_roster(self):
        self.write_control({"mode": "garbage", "sessionId": "ses_NEW0000000000000001"})
        result = self.add(count=1, request_key="w5-scale-004")
        self.assertFalse(result["rosterChanged"])
        self.assertEqual(len(self.roster()), 1)
        with sqlite3.connect(self.native) as db:
            rows = db.execute("SELECT count(*) FROM session_v2").fetchone()[0]
        self.assertEqual(rows, 1)  # the pre-existing session only; nothing fabricated


class ScopeRefusalTests(Sandbox):
    def test_a_wrong_root_is_refused(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        with self.assertRaises(ValueError) as error:
            self.add(count=1, root=other)
        self.assertIn("fleet root", str(error.exception))
        self.assertEqual(self.requests(), [])

    def test_a_wrong_account_is_refused(self):
        other = Path(self.tmp.name) / "other.db"
        with self.assertRaises(ValueError) as error:
            self.add(count=1, database_path=str(other))
        self.assertIn("account", str(error.exception))
        self.assertEqual(self.requests(), [])

    def test_a_malformed_count_is_refused(self):
        for count in (0, -1, opencode_lifecycle.MAX_ADD_COUNT + 1, "2"):
            with self.assertRaises(ValueError):
                self.add(count=count)
        self.assertEqual(self.requests(), [])

    def test_a_malformed_admission_limit_is_refused(self):
        for limit in (1, 3, 65, -1):
            with self.assertRaises(ValueError):
                self.add(count=1, max_concurrent=limit)
        self.assertEqual(self.requests(), [])
        self.assertEqual(paths.config()["maxConcurrent"], 4)

    def test_an_unreadable_configuration_is_refused(self):
        paths.CONFIG.write_text("{not json")
        with self.assertRaises(ValueError):
            self.add(count=1)
        self.assertEqual(self.requests(), [])

    def test_a_cwd_outside_the_worktree_root_is_refused(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        subprocess.run(["git", "init", "-q", str(outside)], check=True)
        with self.assertRaises(ValueError):
            self.add(count=1, cwd=str(outside))
        self.assertEqual(self.requests(), [])

    def test_the_admission_limit_is_never_raised_implicitly(self):
        result = self.add(count=2)
        self.assertEqual(result["maxConcurrent"]["before"], 4)
        self.assertEqual(result["maxConcurrent"]["after"], 4)
        self.assertEqual(paths.config()["maxConcurrent"], 4)

    def test_an_explicitly_validated_admission_limit_is_applied(self):
        result = self.add(count=2, max_concurrent=6)
        self.assertEqual(result["maxConcurrent"]["after"], 6)
        self.assertEqual(paths.config()["maxConcurrent"], 6)

    def test_logical_sessions_are_distinct_from_the_heavy_job_admission(self):
        result = self.add(count=3)
        self.assertEqual(result["logicalSessions"], 4)
        self.assertEqual(result["heavyJobSlots"], 2)
        self.assertEqual(heavy.SLOT_COUNT, 2)


class DrainTests(Sandbox):
    def test_drain_marks_the_worker_and_reports_outstanding_work(self):
        self.task("t-queued", self.existing, "queued")
        result = opencode_lifecycle.drain_session(self.db, self.existing, note="scaling down")
        self.assertTrue(result["draining"])
        self.assertEqual([item["taskId"] for item in result["outstanding"]], ["t-queued"])
        self.assertFalse(result["removed"])
        self.assertTrue(self.roster()[0]["draining"])

    def test_drain_never_cancels_drops_or_releases_a_queued_task(self):
        self.task("t-queued", self.existing, "queued")
        self.db.execute("INSERT INTO task_holds VALUES ('t-queued','held','held',1,1)")
        self.db.commit()
        opencode_lifecycle.drain_session(self.db, self.existing, note="draining")
        state = self.db.execute("SELECT state FROM tasks WHERE id='t-queued'").fetchone()[0]
        self.assertEqual(state, "queued")
        hold = self.db.execute("SELECT status FROM task_holds WHERE task_id='t-queued'").fetchone()
        self.assertEqual(hold[0], "held")

    def test_a_draining_worker_takes_no_new_work(self):
        self.cfg["opencode"][0]["draining"] = True
        self.write_config(self.cfg)
        self.task("t-queued", self.existing, "queued")
        slots = {self.existing: dict(self.roster()[0], provider="opencode")}
        states = {self.existing: {"state": "idle", "cwd": str(self.wt)}}
        calls = []
        dispatch.dispatch(self.db, slots, states, lambda *a: calls.append(a))
        self.assertEqual(calls, [])
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id='t-queued'").fetchone()[0],
                         "queued")

    def test_drain_is_idempotent(self):
        first = opencode_lifecycle.drain_session(self.db, self.existing)
        second = opencode_lifecycle.drain_session(self.db, self.existing)
        self.assertFalse(first["alreadyDraining"])
        self.assertTrue(second["alreadyDraining"])


class RemovalRefusalTests(Sandbox):
    def test_removal_refuses_while_a_task_is_queued(self):
        self.task("t-queued", self.existing, "queued")
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing, note="remove")
        self.assertIn("outstanding work", str(error.exception))
        self.assertEqual(len(self.roster()), 1)

    def test_removal_refuses_while_a_task_is_in_flight(self):
        self.task("t-running", self.existing, "running")
        with self.assertRaises(ValueError):
            opencode_lifecycle.remove_session(self.db, self.existing)
        self.assertEqual(len(self.roster()), 1)

    def test_removal_refuses_while_a_task_is_uncertain(self):
        self.task("t-uncertain", self.existing, "uncertain")
        with self.assertRaises(ValueError):
            opencode_lifecycle.remove_session(self.db, self.existing)
        self.assertEqual(len(self.roster()), 1)

    def test_removal_refuses_while_a_recovery_holds_the_session(self):
        self.db.execute(
            "INSERT INTO recoveries (id,failed_sid,failed_turn,failed_task_id,owner_sid,"
            "owner_task_id,ownership_task_id,cwd,owned_worktree,branch,head,worktree_sha256,"
            "adoption_id,direct_turn_receipt,direct_turn_sha256,note,status,created,updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("r-1", self.existing, "turn-1", "t-old", self.existing, "t-owner", "t-owner",
             str(self.wt), "", "main", "head", "sha", "", "", "", "note", "claimed", 1, 1))
        self.db.commit()
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing)
        self.assertIn("recovery", str(error.exception))

    def test_removal_refuses_when_the_native_identity_cannot_be_verified(self):
        with sqlite3.connect(self.native) as db:
            db.execute("DELETE FROM session_v2 WHERE id=?", (self.existing,))
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing)
        self.assertIn("verified", str(error.exception))
        self.assertEqual(len(self.roster()), 1)

    def test_the_operator_override_is_recorded_and_removes_the_roster_entry(self):
        with sqlite3.connect(self.native) as db:
            db.execute("DELETE FROM session_v2 WHERE id=?", (self.existing,))
        result = opencode_lifecycle.remove_session(self.db, self.existing, note="operator decision",
                                                    allow_unverified_native=True)
        self.assertTrue(result["removed"])
        self.assertEqual(self.roster(), [])

    def test_removal_keeps_the_native_session_and_every_hold(self):
        self.db.execute("INSERT INTO task_holds VALUES ('t-other','held','held',1,1)")
        self.db.commit()
        result = opencode_lifecycle.remove_session(self.db, self.existing, note="retiring")
        self.assertTrue(result["nativeSessionKept"])
        with sqlite3.connect(self.native) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM session_v2 WHERE id=?", (self.existing,)).fetchone()[0],
                1)
        self.assertEqual(self.db.execute("SELECT status FROM task_holds WHERE task_id='t-other'")
                         .fetchone()[0], "held")
        self.assertEqual(self.db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)


class RestartAndOwnershipTests(Sandbox):
    def test_the_receipts_and_holds_survive_a_manager_restart(self):
        self.add(count=1, request_key="w5-scale-005")
        self.db.execute("INSERT INTO task_holds VALUES ('t-queued','held','held',1,1)")
        self.db.commit()
        self.db.close()
        reopened = store.database(paths.DB)
        self.addCleanup(reopened.close)
        receipts = opencode_lifecycle._session_receipts(reopened, "w5-scale-005")
        self.assertEqual([receipt["status"] for receipt in receipts], ["registered"])
        self.assertEqual(reopened.execute("SELECT status FROM task_holds WHERE task_id='t-queued'")
                         .fetchone()[0], "held")
        self.assertEqual(len(opencode_lifecycle.status(reopened)["roster"]), 2)

    def test_a_restarted_request_is_still_idempotent(self):
        self.add(count=1, request_key="w5-scale-006")
        self.db.close()
        reopened = store.database(paths.DB)
        self.addCleanup(reopened.close)
        again = opencode_lifecycle.add_sessions(
            reopened, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-scale-006")
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.requests()), 1)


class ManagerTests(Sandbox):
    def test_the_command_never_starts_a_manager(self):
        with patch.object(ops, "start_manager") as start:
            self.add(count=1)
        start.assert_not_called()

    def test_the_running_manager_is_reported_and_left_alone(self):
        paths.MANAGER_LOCK.parent.mkdir(parents=True, exist_ok=True)
        with paths.MANAGER_LOCK.open("a") as lock:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            report = opencode_lifecycle.status(self.db)
        self.assertTrue(report["managerRunning"])
        self.assertEqual(len(report["roster"]), 1)

    def test_the_status_report_is_read_only(self):
        before = paths.CONFIG.read_bytes()
        report = opencode_lifecycle.status(self.db)
        self.assertEqual(paths.CONFIG.read_bytes(), before)
        self.assertEqual(report["heavyJobSlots"], 2)
        self.assertEqual(report["logicalSessions"], 1)


class DryRunTests(Sandbox):
    def test_a_dry_run_reports_the_plan_and_writes_nothing(self):
        before = paths.CONFIG.read_bytes()
        result = self.add(count=2, dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["toCreate"], 2)
        self.assertEqual(result["created"], 0)
        self.assertEqual(self.requests(), [])
        self.assertEqual(paths.CONFIG.read_bytes(), before)
        self.assertEqual(len(self.roster()), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM opencode_sessions").fetchone()[0], 0)

    def test_the_status_report_names_the_account_and_the_receipts(self):
        report = opencode_lifecycle.status(self.db)
        self.assertEqual(report["accounts"], [str(self.native)])
        self.assertEqual(report["logicalSessions"], 1)
        self.assertEqual(report["admissionLimit"], 4)
        self.assertEqual(report["heavyJobSlots"], 2)
        self.assertEqual(report["roster"][0]["sessionId"], self.existing)
        self.assertFalse(report["roster"][0]["draining"])


class InterruptionTests(Sandbox):
    def test_a_hung_creation_is_uncertain_and_never_writes_the_roster(self):
        self.write_control({"mode": "hang", "sessionId": "ses_NEW0000000000000001"})
        with self.assertRaises(opencode_lifecycle.ProvisioningUncertain):
            opencode_lifecycle.create_native_session(
                str(self.fake), str(self.native), str(self.wt), MODEL,
                self.root / "scratch", timeout=1)
        self.assertEqual(len(self.roster()), 1)

    def test_a_creation_that_answers_with_garbage_is_refused(self):
        self.write_control({"mode": "garbage", "sessionId": "ses_NEW0000000000000001"})
        with self.assertRaises(opencode_lifecycle.ProvisioningError):
            opencode_lifecycle.create_native_session(
                str(self.fake), str(self.native), str(self.wt), MODEL, self.root / "scratch")
        self.assertEqual(len(self.roster()), 1)

    def test_the_worktree_of_an_existing_worker_is_never_touched(self):
        marker = self.wt / "marker.txt"
        marker.write_text("untouched")
        before = sorted(path.name for path in self.wt.iterdir())
        self.add(count=1)
        opencode_lifecycle.drain_session(self.db, self.existing, note="draining")
        opencode_lifecycle.remove_session(self.db, self.existing, note="retiring")
        self.assertEqual(marker.read_text(), "untouched")
        self.assertEqual(sorted(path.name for path in self.wt.iterdir()), before)


class UncertainOutcomeTests(Sandbox):
    """An uncertain outcome stays uncertain until the native identity is reconciled."""

    def test_a_hung_creation_is_recorded_as_uncertain_and_not_as_a_failure(self):
        self.write_control({"mode": "hang", "sessionId": "ses_NEW0000000000000001"})
        with patch.object(opencode_lifecycle, "CREATE_TIMEOUT_SECONDS", 1):
            result = self.add(count=1, request_key="w5-uncertain-001")
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["retryable"])
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["rosterChanged"])
        row = self.db.execute("SELECT status,detail FROM opencode_provisioning "
                              "WHERE request_key=?", ("w5-uncertain-001",)).fetchone()
        self.assertEqual(row[0], "uncertain")
        self.assertEqual(len(self.roster()), 1)

    def test_the_intent_is_durable_before_the_first_native_call(self):
        seen = []
        original = opencode_lifecycle._record_provisioning

        def record(db, key, request, status, detail="", count=None, payload_digest=""):
            seen.append(status)
            return original(db, key, request, status, detail, count, payload_digest)

        with patch.object(opencode_lifecycle, "_record_provisioning", side_effect=record):
            self.add(count=1, request_key="w5-uncertain-002")
        self.assertEqual(seen[0], "creating")
        self.assertIn("registered", seen)

    def test_a_crash_after_the_native_create_is_reconciled_before_a_retry(self):
        # The native session is created, then the process dies before the receipt.
        with patch.object(opencode_lifecycle, "_record_session", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.add(count=1, request_key="w5-uncertain-003")
        self.assertEqual(len(self.roster()), 1)
        with sqlite3.connect(self.native) as db:
            created = db.execute("SELECT id FROM session_v2 WHERE id<>?",
                                 (self.existing,)).fetchall()
        self.assertEqual(len(created), 1)  # the native session exists
        receipts = opencode_lifecycle._session_receipts(self.db, "w5-uncertain-003")
        self.assertEqual(receipts, [])  # and it was never receipted
        calls_before = len(self.requests())
        resumed = self.add(count=1, request_key="w5-uncertain-003")
        # The retry adopts the session that already exists: no second native session.
        self.assertEqual(resumed["created"], 0)
        self.assertEqual(resumed["reconciled"], [created[0][0]])
        self.assertEqual(len(self.requests()), calls_before)
        self.assertEqual([entry["sessionId"] for entry in self.roster()],
                         [self.existing, created[0][0]])

    def test_an_unverified_receipt_is_never_ignored(self):
        self.db.execute(
            "INSERT INTO opencode_sessions (session_id,request_key,native_id,model,account,cwd,"
            "root,status,created,updated,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("ses_UNVERIFIED0000000001", "w5-uncertain-004", "ses_UNVERIFIED0000000001",
             MODEL, str(self.native), str(self.wt), str(self.root), "unverified", 1, 1, "x"))
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("w5-uncertain-004", MODEL, str(self.native), str(self.wt), str(self.root), 1, 0,
             "creating", 1, 1, "", ""))
        self.db.commit()
        with self.assertRaises(ValueError) as error:
            self.add(count=1, request_key="w5-uncertain-004")
        self.assertIn("not ignored", str(error.exception))
        self.assertEqual(self.requests(), [])

    def test_an_unverified_receipt_is_promoted_when_the_identity_can_be_read_back(self):
        identifier = "ses_UNVERIFIED0000000002"
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       (identifier, str(self.wt), "project-test", MODEL))
        self.db.execute(
            "INSERT INTO opencode_sessions (session_id,request_key,native_id,model,account,cwd,"
            "root,status,created,updated,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (identifier, "w5-uncertain-005", identifier, MODEL, str(self.native), str(self.wt),
             str(self.root), "unverified", 1, 1, "x"))
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("w5-uncertain-005", MODEL, str(self.native), str(self.wt), str(self.root), 1, 0,
             "creating", 1, 1, "", ""))
        self.db.commit()
        result = self.add(count=1, request_key="w5-uncertain-005")
        self.assertEqual(result["created"], 0)
        self.assertEqual([entry["sessionId"] for entry in self.roster()], [self.existing, identifier])
        receipt = self.db.execute("SELECT status FROM opencode_sessions WHERE session_id=?",
                                  (identifier,)).fetchone()
        self.assertEqual(receipt[0], "registered")


class SerializationTests(Sandbox):
    """Same-key provisioning and roster writes are serialized and compared."""

    def test_two_concurrent_requests_with_the_same_key_create_one_session(self):
        import threading
        results = []

        def run():
            db = store.database(paths.DB)
            try:
                results.append(opencode_lifecycle.add_sessions(
                    db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                    request_key="w5-race-001"))
            except Exception as error:  # noqa: BLE001 - the failure is the result
                results.append({"error": str(error)})
            finally:
                db.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.roster()), 2)
        self.assertEqual(sorted(result.get("created", -1) for result in results), [0, 1])

    def test_a_configuration_changed_during_the_request_is_never_overwritten(self):
        def rewrite(*args, **kwargs):
            # A concurrent writer changes the roster while the request runs.
            cfg = paths.config()
            cfg["maxConcurrent"] = 7
            paths.write_config(cfg)
            return {"id": "ses_RACE0000000000000001", "data": {}}

        with patch.object(opencode_lifecycle, "create_native_session",
                          side_effect=rewrite), \
             patch.object(opencode_lifecycle, "_verify_created", return_value={}):
            with self.assertRaises(ValueError) as error:
                self.add(count=1, request_key="w5-race-002")
        self.assertIn("configuration changed", str(error.exception))
        self.assertEqual(paths.config()["maxConcurrent"], 7)
        self.assertEqual(len(self.roster()), 1)

    def test_a_removal_and_a_dispatch_claim_cannot_both_win(self):
        import threading
        started = threading.Event()
        release = threading.Event()

        def claim():
            # A dispatch claim holds the model lock, then a task appears for the
            # worker it claimed.
            db = store.database(paths.DB)
            try:
                with model_selection.model_lock():
                    started.set()
                    release.wait(10)
                    db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                               ("t-claimed", self.existing, str(self.wt),
                                json.dumps({"message": "m"}), "sent", 1))
                    db.commit()
            finally:
                db.close()

        thread = threading.Thread(target=claim)
        thread.start()
        started.wait(10)
        outcome = {}

        def remove():
            db = store.database(paths.DB)
            try:
                outcome["result"] = opencode_lifecycle.remove_session(db, self.existing)
            except ValueError as error:
                outcome["error"] = str(error)
            finally:
                db.close()

        remover = threading.Thread(target=remove)
        remover.start()
        release.set()
        thread.join(20)
        remover.join(20)
        self.assertIn("error", outcome)
        self.assertIn("outstanding work", outcome["error"])
        self.assertEqual(len(self.roster()), 1)


class RequestKeyBindingTests(Sandbox):
    """A request key is bound to the complete request."""

    def test_the_same_key_with_a_different_count_is_refused(self):
        self.add(count=1, request_key="w5-key-001")
        with self.assertRaises(ValueError) as error:
            self.add(count=2, request_key="w5-key-001")
        self.assertIn("different request", str(error.exception))
        self.assertEqual(len(self.requests()), 1)

    def test_the_same_key_with_a_different_dispatch_flag_is_refused(self):
        self.add(count=1, request_key="w5-key-002")
        with self.assertRaises(ValueError):
            self.add(count=1, request_key="w5-key-002", enable_dispatch=True)

    def test_the_same_key_with_a_different_admission_limit_is_refused(self):
        self.add(count=1, request_key="w5-key-003")
        with self.assertRaises(ValueError):
            self.add(count=1, request_key="w5-key-003", max_concurrent=6)

    def test_the_same_key_with_a_different_model_is_refused(self):
        self.add(count=1, request_key="w5-key-004")
        with self.assertRaises(ValueError):
            self.add(count=1, request_key="w5-key-004", model=OTHER_APPROVED)

    def test_the_same_key_with_a_different_account_is_refused(self):
        self.add(count=1, request_key="w5-key-005")
        other = Path(self.tmp.name) / "other.db"
        with self.assertRaises(ValueError):
            self.add(count=1, request_key="w5-key-005", database_path=str(other))

    def test_the_same_key_with_a_different_working_directory_is_refused(self):
        self.add(count=1, request_key="w5-key-006")
        other = Path(self.tmp.name) / "wt" / "second"
        other.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        with self.assertRaises(ValueError):
            self.add(count=1, request_key="w5-key-006", cwd=str(other))

    def test_the_same_request_with_the_same_options_is_idempotent(self):
        self.add(count=1, request_key="w5-key-007")
        again = self.add(count=1, request_key="w5-key-007")
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.requests()), 1)


class OverrideTests(Sandbox):
    """The operator override never covers a worker that is known to be active."""

    def test_a_worker_that_is_running_is_never_removed_even_with_the_override(self):
        with patch.object(opencode_lifecycle, "_native_state",
                          return_value={"state": "running", "cwd": str(self.wt)}):
            with self.assertRaises(ValueError) as error:
                opencode_lifecycle.remove_session(self.db, self.existing,
                                                 allow_unverified_native=True)
        self.assertIn("known to be active", str(error.exception))
        self.assertEqual(len(self.roster()), 1)

    def test_an_unverifiable_worker_may_be_removed_with_the_override(self):
        with patch.object(opencode_lifecycle, "_native_state",
                          return_value={"state": "unavailable", "error": "x"}):
            result = opencode_lifecycle.remove_session(self.db, self.existing,
                                                        allow_unverified_native=True)
        self.assertTrue(result["removed"])


class BoundedRequestTests(Sandbox):
    """One bounded request covers a whole fleet scale-up."""

    def test_one_request_adds_seventeen_sessions(self):
        self.write_control({"mode": "ok", "sessionId": "ses_SCALE000000000000000",
                            "unique": True})
        result = self.add(count=17, request_key="w5-scale-17")
        self.assertEqual(result["created"], 17)
        self.assertEqual(len(self.roster()), 18)
        self.assertEqual(len(self.requests()), 17)

    def test_the_bound_is_still_a_bound(self):
        with self.assertRaises(ValueError):
            self.add(count=opencode_lifecycle.MAX_ADD_COUNT + 1)
        self.assertEqual(opencode_lifecycle.MAX_ADD_COUNT, 32)


class RollbackTests(Sandbox):
    """A request can be withdrawn without deleting a native session."""

    def test_a_rollback_withdraws_the_roster_entries_and_keeps_the_sessions(self):
        self.write_control({"mode": "ok", "sessionId": "ses_ROLLBACK000000000001",
                            "unique": True})
        self.add(count=2, request_key="w5-rollback-001")
        self.assertEqual(len(self.roster()), 3)
        result = opencode_lifecycle.rollback_request(self.db, "w5-rollback-001", note="reverting")
        self.assertEqual(result["removedFromRoster"],
                         ["ses_ROLLBACK000000000001-0", "ses_ROLLBACK000000000001-1"])
        self.assertEqual(len(self.roster()), 1)
        with sqlite3.connect(self.native) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM session_v2").fetchone()[0], 3)

    def test_a_rolled_back_key_never_adds_again(self):
        self.write_control({"mode": "ok", "sessionId": "ses_ROLLBACK000000000002",
                            "unique": True})
        self.add(count=1, request_key="w5-rollback-002")
        opencode_lifecycle.rollback_request(self.db, "w5-rollback-002")
        with self.assertRaises(ValueError) as error:
            self.add(count=1, request_key="w5-rollback-002")
        self.assertIn("rolled-back", str(error.exception))
        self.assertEqual(len(self.roster()), 1)


class PlanTests(Sandbox):
    """The staged activation and rollback plan rests on these invariants."""

    def test_a_store_created_before_the_receipt_tables_gains_them(self):
        legacy = Path(self.tmp.name) / "legacy.sqlite"
        connection = sqlite3.connect(legacy)
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, sid TEXT NOT NULL, "
                           "cwd TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL, "
                           "updated REAL NOT NULL)")
        connection.execute("INSERT INTO tasks VALUES ('t-legacy','s','/w','{}','queued',1)")
        connection.commit()
        connection.close()
        db = store.database(legacy)
        self.addCleanup(db.close)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("opencode_provisioning", tables)
        self.assertIn("opencode_sessions", tables)
        self.assertEqual(db.execute("SELECT state FROM tasks WHERE id='t-legacy'").fetchone()[0],
                         "queued")

    def test_a_provisioning_row_created_before_the_digest_column_gains_it(self):
        legacy = Path(self.tmp.name) / "legacy-provisioning.sqlite"
        connection = sqlite3.connect(legacy)
        connection.execute("CREATE TABLE opencode_provisioning (request_key TEXT PRIMARY KEY, "
                           "model TEXT NOT NULL, account TEXT NOT NULL, cwd TEXT NOT NULL, "
                           "root TEXT NOT NULL, requested_count INTEGER NOT NULL, "
                           "dispatch_enabled INTEGER NOT NULL, status TEXT NOT NULL, "
                           "created REAL NOT NULL, updated REAL NOT NULL, "
                           "detail TEXT NOT NULL DEFAULT '')")
        connection.commit()
        connection.close()
        db = store.database(legacy)
        self.addCleanup(db.close)
        columns = {row[1] for row in db.execute("PRAGMA table_info(opencode_provisioning)")}
        self.assertIn("payload_digest", columns)

    def test_the_drain_reports_that_an_older_manager_does_not_honour_it(self):
        result = opencode_lifecycle.drain_session(self.db, self.existing, note="draining")
        self.assertTrue(result["draining"])
        self.assertIn("predates", result["advisory"])
        self.assertIn("honouredByThisBuild", result)
        # This build's dispatcher honours the flag; an older pinned manager does not.
        self.assertTrue(result["honouredByThisBuild"])

    def test_the_removal_refuses_while_work_is_outstanding_even_when_draining(self):
        self.task("t-queued", self.existing, "queued")
        opencode_lifecycle.drain_session(self.db, self.existing, note="draining")
        with self.assertRaises(ValueError):
            opencode_lifecycle.remove_session(self.db, self.existing)
        self.assertEqual(len(self.roster()), 1)


if __name__ == "__main__":
    unittest.main()
