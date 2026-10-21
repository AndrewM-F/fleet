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

from fleet import dispatch, heavy, opencode, opencode_lifecycle, ops, paths, store  # noqa: E402

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
        for count in (0, -1, 17, "2"):
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


if __name__ == "__main__":
    unittest.main()
