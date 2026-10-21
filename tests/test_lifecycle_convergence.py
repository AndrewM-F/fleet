"""The lifecycle converges: what a command reports is what the durable state holds.

Three gaps are covered here, all of them found against the published head
`17890950`:

* an idempotent result counted the sessions it had registered and then listed
  none of them, so the counts and the list disagreed;
* an idempotent result listed a session the roster no longer held, so it
  reported a worker that is gone;
* a removal that was interrupted between the roster write and the receipt
  write left a receipt claiming a worker the roster no longer had, and the
  removal could not be repeated: it refused, because the worker was gone.
  The record and the roster could therefore never agree again.

Synthetic only: a temporary fleet root, a synthetic native session inventory
and the same fake OpenCode CLI the rest of the suite uses. No native session is
created outside the sandbox, no manager is activated, and no account, quota or
live configuration is touched.
"""
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fleet import opencode_lifecycle, paths  # noqa: E402
from test_lifecycle_findings import FindingSandbox  # noqa: E402

MODEL = "opencode-go/minimax-m3.1-flash-free"
# Above the default pid ceiling: it cannot exist, so it models a creator whose
# process is gone without weakening the runtime's own liveness check.
DEAD_PID = 4194303


class LifecycleSandbox(FindingSandbox):
    """The shared fixtures: one added worker, and the helpers the tests use."""

    def status_of(self, session_id):
        row = self.db.execute("SELECT status FROM opencode_sessions WHERE session_id=?",
                              (session_id,)).fetchone()
        return row[0] if row is not None else None

    def roster_ids(self):
        return [entry.get("sessionId") for entry in paths.config().get("opencode", [])]

    def add_one(self, key):
        result = opencode_lifecycle.add_sessions(
            self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key=key)
        self.assertEqual(result["created"], 1)
        return result["sessions"][0]["sessionId"]

    def request(self, key):
        return {"model": MODEL, "account": str(self.native), "cwd": str(self.wt),
                "root": str(self.root), "count": 1, "dispatchEnabled": False}

    def task(self, task_id, sid, state):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (task_id, sid, str(self.wt), json.dumps({"message": "m"}), state, 1))
        self.db.commit()

    def interrupt_the_request_row(self, key, detail):
        """Leave the request row as a crash would leave it.

        The row is written directly, because the rule under repair refuses to
        move a registered request backwards: that refusal is the point.
        """
        self.db.execute("UPDATE opencode_provisioning SET status='creating',detail=?,"
                        "creator_pid=? WHERE request_key=?", (detail, DEAD_PID, key))
        self.db.commit()

    def native_has(self, session_id):
        import sqlite3
        connection = sqlite3.connect(f"file:{self.native}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT 1 FROM session_v2 WHERE id=?",
                                     (session_id,)).fetchone()
        finally:
            connection.close()
        return row is not None


class IdempotentResultTests(LifecycleSandbox):
    """An idempotent result counts exactly the sessions it lists."""

    def test_the_result_counts_the_sessions_it_lists_after_an_interrupted_request_row(self):
        identifier = self.add_one("w5-conv-001")
        # The roster write and the receipts happened; the request row did not.
        # That is the window a crash leaves behind.
        self.interrupt_the_request_row("w5-conv-001", json.dumps({"sessions": [identifier]}))
        self.assertEqual(opencode_lifecycle._provisioning_row(self.db, "w5-conv-001")[7],
                         "creating")
        again = opencode_lifecycle.add_sessions(
            self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-conv-001")
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["registered"], 1)
        self.assertEqual([item["sessionId"] for item in again["sessions"]], [identifier])
        self.assertEqual(again["registered"], len(again["sessions"]))

    def test_the_result_never_lists_a_worker_the_roster_does_not_hold(self):
        identifier = self.add_one("w5-conv-002")
        # The worker leaves the roster while its receipt still says registered.
        config = paths.config()
        config["opencode"] = [entry for entry in config["opencode"]
                              if entry.get("sessionId") != identifier]
        self.write_config(config)
        again = opencode_lifecycle.add_sessions(
            self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-conv-002")
        self.assertEqual(again["registered"], 0)
        self.assertEqual(again["sessions"], [])
        self.assertEqual(again.get("notInRoster"), [identifier])
        self.assertEqual(again["registered"], len(again["sessions"]))


class RemovalConvergenceTests(LifecycleSandbox):
    """A removal that was interrupted is completed, with the same barriers."""

    def interrupt_the_removal(self, identifier):
        original = opencode_lifecycle._record_session

        def fail_on_removed(db, session_id, request_key, request, status, detail=""):
            if status == "removed":
                raise KeyboardInterrupt("the receipt update was interrupted")
            return original(db, session_id, request_key, request, status, detail)

        with patch.object(opencode_lifecycle, "_record_session", side_effect=fail_on_removed):
            with self.assertRaises(KeyboardInterrupt):
                opencode_lifecycle.remove_session(self.db, identifier, note="remove")

    def test_an_interrupted_removal_is_completed_instead_of_refused(self):
        identifier = self.add_one("w5-conv-010")
        self.interrupt_the_removal(identifier)
        # The roster no longer holds the worker; the receipt still claims it.
        self.assertNotIn(identifier, self.roster_ids())
        self.assertEqual(self.status_of(identifier), "registered")
        # The removal is repeated and converges.
        again = opencode_lifecycle.remove_session(self.db, identifier, note="remove")
        self.assertTrue(again["removed"])
        self.assertFalse(again["rosterChanged"])
        self.assertEqual(self.status_of(identifier), "removed")
        self.assertNotIn(identifier, self.roster_ids())
        # The native session and its history are untouched.
        self.assertTrue(again["nativeSessionKept"])
        self.assertTrue(self.native_has(identifier))

    def test_the_completed_removal_still_refuses_while_work_is_outstanding(self):
        identifier = self.add_one("w5-conv-011")
        self.interrupt_the_removal(identifier)
        self.task("t-conv-011", identifier, "queued")
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, identifier, note="remove")
        self.assertIn("outstanding work", str(error.exception))
        self.assertEqual(self.status_of(identifier), "registered")

    def test_the_completed_removal_still_refuses_an_unreadable_queue(self):
        identifier = self.add_one("w5-conv-012")
        self.interrupt_the_removal(identifier)
        paths.QUEUE.write_text("{not json")
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, identifier, note="remove")
        self.assertIn("could not be read", str(error.exception))
        self.assertEqual(self.status_of(identifier), "registered")

    def test_the_completed_removal_still_refuses_a_worker_known_to_be_active(self):
        identifier = self.add_one("w5-conv-013")
        self.interrupt_the_removal(identifier)
        with patch.object(opencode_lifecycle, "_native_state",
                          return_value={"state": "running", "cwd": str(self.wt)}):
            with self.assertRaises(ValueError) as error:
                opencode_lifecycle.remove_session(self.db, identifier, note="remove")
        self.assertIn("known to be active", str(error.exception))
        self.assertEqual(self.status_of(identifier), "registered")

    def test_removing_a_worker_that_is_already_removed_is_idempotent(self):
        identifier = self.add_one("w5-conv-014")
        first = opencode_lifecycle.remove_session(self.db, identifier, note="remove")
        self.assertTrue(first["removed"])
        second = opencode_lifecycle.remove_session(self.db, identifier, note="remove")
        self.assertTrue(second["removed"])
        self.assertTrue(second["alreadyRemoved"])
        self.assertFalse(second["rosterChanged"])
        self.assertEqual(self.status_of(identifier), "removed")

    def test_the_removal_of_a_session_that_was_never_a_worker_still_refuses(self):
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, "ses_NEVER00000000000001", note="remove")
        self.assertIn("not one configured OpenCode worker", str(error.exception))


class RequestRowTests(LifecycleSandbox):
    """A registered request is terminal, whoever writes it."""

    def test_a_later_failure_never_moves_a_registered_request_backwards(self):
        request = self.request("w5-conv-020")
        opencode_lifecycle._record_provisioning(
            self.db, "w5-conv-020", request, "registered", "done",
            payload_digest=self.digest(), creator_pid=os.getpid())
        for status in ("failed", "uncertain", "creating"):
            opencode_lifecycle._record_provisioning(
                self.db, "w5-conv-020", request, status, "a later write",
                payload_digest=self.digest(), creator_pid=os.getpid())
            row = opencode_lifecycle._provisioning_row(self.db, "w5-conv-020")
            self.assertEqual(row[7], "registered")
            self.assertEqual(row[10], "done")


class InterruptedReceiptTests(LifecycleSandbox):
    """A request interrupted between two of its own receipt writes converges."""

    def creations(self):
        if not self.fake_log.exists():
            return []
        return [json.loads(line) for line in self.fake_log.read_text().splitlines()
                if line.strip() and "POST" in line and "/api/session" in line]

    def test_a_request_interrupted_between_two_receipt_writes_converges(self):
        original = opencode_lifecycle._record_session
        state = {"written": 0}

        def fail_on_the_second_registration(db, session_id, request_key, request,
                                            status, detail=""):
            if status == "registered":
                state["written"] += 1
                if state["written"] == 2:
                    raise KeyboardInterrupt("the second receipt was interrupted")
            return original(db, session_id, request_key, request, status, detail)

        with patch.object(opencode_lifecycle, "_record_session",
                          side_effect=fail_on_the_second_registration):
            with self.assertRaises(KeyboardInterrupt):
                opencode_lifecycle.add_sessions(
                    self.db, count=3, model=MODEL, cwd=str(self.wt), root=str(self.root),
                    request_key="w5-conv-021")
        # The roster holds all three sessions; only the first receipt says
        # registered, the other two still say created.
        self.assertEqual(len(self.roster_ids()), 4)
        self.assertEqual(len(self.creations()), 3)
        self.db.execute("UPDATE opencode_provisioning SET creator_pid=? WHERE request_key=?",
                        (DEAD_PID, "w5-conv-021"))
        self.db.commit()
        # The retry converges: no second native session, every receipt registered.
        again = opencode_lifecycle.add_sessions(
            self.db, count=3, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-conv-021")
        self.assertEqual(len(self.creations()), 3)
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["registered"], 3)
        self.assertEqual(len(again["sessions"]), 3)
        self.assertEqual(len(self.roster_ids()), 4)
        statuses = [row[0] for row in self.db.execute(
            "SELECT status FROM opencode_sessions WHERE request_key=? ORDER BY created",
            ("w5-conv-021",))]
        self.assertEqual(statuses, ["registered", "registered", "registered"])
        self.assertEqual(again["proof"]["expected"], 3)
        self.assertEqual(again["proof"]["receipts"], 3)


class AdmissionReportTests(LifecycleSandbox):
    """The result reports the admission limit and the roster size it ends at."""

    def test_the_result_reports_the_admission_limit_in_effect(self):
        result = opencode_lifecycle.add_sessions(
            self.db, count=3, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-conv-030", max_concurrent=8)
        self.assertEqual(result["admissionLimit"], 8)
        self.assertEqual(result["maxConcurrent"], {"before": 4, "after": 8})
        self.assertEqual(paths.config()["maxConcurrent"], 8)

    def seed_pending(self, key, identifiers, count, max_concurrent=None):
        """Two native sessions that exist for this request, and no roster entry."""
        with sqlite3.connect(self.native) as db:
            for identifier in identifiers:
                db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                           (identifier, str(self.wt), "project-test", MODEL))
        for identifier in identifiers:
            self.db.execute(
                "INSERT INTO opencode_sessions (session_id,request_key,native_id,model,account,"
                "cwd,root,status,created,updated,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, key, identifier, MODEL, str(self.native), str(self.wt),
                 str(self.root), "created", 1, 1, "created before the crash"))
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest,"
            "creator_pid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, MODEL, str(self.native), str(self.wt), str(self.root), count, 0, "creating",
             1, 1, json.dumps({"sessions": identifiers}),
             self.digest(count=count, max_concurrent=max_concurrent), DEAD_PID))
        self.db.commit()

    def test_the_recovery_path_reports_the_roster_size_it_ends_at(self):
        key = "w5-conv-031"
        self.seed_pending(key, ["ses_PENDING000000000001", "ses_PENDING000000000002"], 2)
        result = opencode_lifecycle.add_sessions(
            self.db, count=2, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key=key)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["logicalSessions"], 3)
        self.assertEqual(len(self.roster_ids()), 3)

    def test_the_admission_ceiling_is_checked_against_the_roster_size_it_ends_at(self):
        key = "w5-conv-032"
        self.seed_pending(key, ["ses_PENDING000000000003", "ses_PENDING000000000004"], 2,
                          max_concurrent=6)
        result = opencode_lifecycle.add_sessions(
            self.db, count=2, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key=key, max_concurrent=6)
        self.assertEqual(result["admissionLimit"], 6)
        self.assertEqual(result["logicalSessions"], 3)
        self.assertEqual(paths.config()["maxConcurrent"], 6)


class SnapshotBoundaryTests(LifecycleSandbox):
    """A change that lands between the digest and the configuration is detected."""

    def change_the_config_once(self):
        original = paths.config
        state = {"changed": False}

        def config_with_an_out_of_band_change(path=None):
            cfg = original(path)
            if not state["changed"]:
                state["changed"] = True
                changed = json.loads(paths.CONFIG.read_text())
                changed["maxConcurrent"] = 9
                paths.CONFIG.write_text(json.dumps(changed))
            return cfg

        return config_with_an_out_of_band_change

    def test_an_out_of_band_change_at_the_boundary_is_refused_and_kept(self):
        with patch.object(paths, "config", side_effect=self.change_the_config_once()):
            with self.assertRaises(ValueError) as error:
                opencode_lifecycle.add_sessions(
                    self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                    request_key="w5-conv-040")
        self.assertIn("changed while", str(error.exception))
        # The out-of-band change is still there: it was never overwritten.
        self.assertEqual(paths.config()["maxConcurrent"], 9)
        self.assertEqual(len(self.roster_ids()), 1)

    def test_a_drain_refuses_an_out_of_band_change_at_the_boundary(self):
        with patch.object(paths, "config", side_effect=self.change_the_config_once()):
            with self.assertRaises(ValueError) as error:
                opencode_lifecycle.drain_session(self.db, self.existing, note="draining")
        self.assertIn("changed while", str(error.exception))
        self.assertEqual(paths.config()["maxConcurrent"], 9)
        self.assertFalse(paths.config()["opencode"][0].get("draining", False))


class FailureReportTests(LifecycleSandbox):
    """A result never advertises a retry the next run refuses."""

    def test_a_failure_that_leaves_an_unverified_session_is_not_retryable(self):
        self.control.write_text(json.dumps(
            {"mode": "ok", "sessionId": "ses_OTHERMODEL000000001",
             "recordModel": "opencode/deepseek-v4.1-flash-free"}))
        result = opencode_lifecycle.add_sessions(
            self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-conv-050")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["uncertain"])
        self.assertFalse(result["retryable"])
        self.assertIn("could never be verified", result["note"])
        # The next run with the same key refuses it, exactly as the result says.
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.add_sessions(
                self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                request_key="w5-conv-050")
        self.assertIn("could never be verified", str(error.exception))

    def test_a_failure_before_the_command_starts_stays_retryable(self):
        # The command cannot be started at all: nothing ran, so the failure is
        # definite and the request may be retried. The subprocess-level case is
        # covered by the ambiguous-effect suite; this is the request's report.
        with patch.object(opencode_lifecycle, "create_native_session",
                          side_effect=opencode_lifecycle.ProvisioningError(
                              "The OpenCode CLI could not be started: OSError")):
            result = opencode_lifecycle.add_sessions(
                self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                request_key="w5-conv-051")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["note"], "")
        # Nothing was created natively and the roster is untouched.
        self.assertEqual(len(self.roster_ids()), 1)

    def test_a_missing_cli_is_a_command_error_not_a_traceback(self):
        config = paths.config()
        config["executables"]["opencode"] = str(self.root / "missing-opencode")
        self.write_config(config)
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.add_sessions(
                self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                request_key="w5-conv-052")
        self.assertIn("could not be found", str(error.exception))


class ResultTruthfulnessTests(LifecycleSandbox):
    """The idempotent result names every session of the request."""

    def test_the_idempotent_result_names_the_draining_sessions(self):
        identifier = self.add_one("w5-conv-060")
        opencode_lifecycle.drain_session(self.db, identifier, note="draining")
        again = opencode_lifecycle.add_sessions(
            self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-conv-060")
        self.assertEqual(again["registered"], 0)
        self.assertEqual(again["sessions"], [])
        self.assertEqual(again["draining"], [identifier])
        self.assertEqual(again["notInRoster"], [])
        self.assertEqual(again["removed"], [])

    def test_a_dry_run_counts_a_session_it_would_reconcile(self):
        identifier = "ses_UNVERIFIED00000000001"
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       (identifier, str(self.wt), "project-test", MODEL))
        self.db.execute(
            "INSERT INTO opencode_sessions (session_id,request_key,native_id,model,account,"
            "cwd,root,status,created,updated,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (identifier, "w5-conv-070", identifier, MODEL, str(self.native), str(self.wt),
             str(self.root), "unverified", 1, 1, "the identity was never read back"))
        self.db.commit()
        plan = opencode_lifecycle.add_sessions(
            self.db, count=2, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-conv-070", dry_run=True)
        # One session exists natively and would be reconciled; one is created.
        self.assertEqual(plan["toCreate"], 1)
        self.assertEqual(plan["pendingNative"], 1)
        self.assertEqual(plan["logicalSessions"], 3)

    def test_a_dry_run_counts_a_named_session_as_reconciled_not_created(self):
        identifier = "ses_NAMED000000000000001"
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       (identifier, str(self.wt), "project-test", MODEL))
        plan = opencode_lifecycle.add_sessions(
            self.db, count=2, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-conv-071", reconciled_sessions=[identifier], dry_run=True)
        self.assertEqual(plan["toCreate"], 1)
        self.assertEqual(plan["pendingNative"], 1)
        self.assertEqual(plan["reconciled"], [identifier])
        # A dry run writes nothing at all.
        self.assertEqual(self.db.execute("SELECT count(*) FROM opencode_sessions "
                                         "WHERE session_id=?", (identifier,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
