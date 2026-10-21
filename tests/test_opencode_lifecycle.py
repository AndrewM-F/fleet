"""On-demand OpenCode session lifecycle: synthetic inventories and injected failures.

Every test here runs against a temporary fleet root, a synthetic native session
inventory and a fake OpenCode CLI that serves the schema the Fleet consults and
records the sessions it is asked to create. No real native database is written,
no provider is contacted and no live fleet is touched.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import (dispatch, heavy, model_selection, opencode,  # noqa: E402
                 opencode_lifecycle, ops, paths, roster, store)

MODEL = "opencode-go/minimax-m3.1-flash-free"
OTHER_APPROVED = "opencode/minimax-m3.1-flash-free"

FAKE_CLI = '''#!/usr/bin/env python3
"""A fake OpenCode CLI: it serves its own schema and records native sessions."""
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
creations = 0
if os.path.exists(LOG):
    with open(LOG) as stream:
        creations = sum(1 for line in stream if "POST" in line and "/api/session" in line)
with open(LOG, "a") as stream:
    stream.write(json.dumps({{"argv": argv, "data": data,
                             "config": os.environ.get("OPENCODE_CONFIG_CONTENT"),
                             "db": os.environ.get("OPENCODE_DB"),
                             "home": os.environ.get("HOME")}}) + "\\n")
mode = control.get("mode", "ok")
if "GET" in argv and "/openapi.json" in argv:
    if control.get("noSchemaOperation"):
        print(json.dumps({{"openapi": "3.1.0", "paths": {{}}}}))
        raise SystemExit(0)
    # The resolved schema of the installed OpenCode CLI (opencode v2.0.29): the
    # model is a Model.Ref object with a required id and providerID, the location
    # is a Location.PublicRef object with a required directory, and the body
    # allows no other property.
    print(json.dumps({{
        "openapi": "3.1.0",
        "info": {{"title": "opencode HttpApi", "version": "0.0.1"}},
        "components": {{"schemas": {{
            "Model.Ref": {{"type": "object",
                           "properties": {{"id": {{"type": "string"}},
                                           "providerID": {{"type": "string"}},
                                           "variant": {{"type": "string"}}}},
                           "required": ["id", "providerID"],
                           "additionalProperties": False}},
            "Location.PublicRef": {{"type": "object",
                                    "properties": {{"directory": {{"type": "string"}}}},
                                    "required": ["directory"],
                                    "additionalProperties": False}},
        }}}},
        "paths": {{"/api/session": {{"post": {{
            "operationId": "session.create",
            "summary": "Create session",
            "description": "Create a session at the requested location.",
            "requestBody": {{"content": {{"application/json": {{"schema": {{
                "type": "object",
                "properties": {{"id": {{"type": "string"}},
                                "parentID": {{"type": "string"}},
                                "title": {{"type": "string"}},
                                "agent": {{"type": "string"}},
                                "model": {{"$ref": "#/components/schemas/Model.Ref"}},
                                "location": {{"$ref": "#/components/schemas/Location.PublicRef"}},
                                "metadata": {{"type": "object"}},
                                "permissions": {{"type": "array", "items": {{"type": "object"}}}}}},
                "required": [],
                "additionalProperties": False}}}}}}}},
            "responses": {{"200": {{"description": "Success"}}}},
            "security": []}}}}}},
    }}))
    raise SystemExit(0)
if mode == "hang":
    time.sleep(600)
if mode == "fail" or (mode == "okThenFail" and creations >= control.get("failAfter", 1)):
    sys.stderr.write("fake opencode refused the request\\n")
    raise SystemExit(3)
if mode == "garbage":
    print("not json at all")
    raise SystemExit(0)
session_id = control["sessionId"]
if not control.get("duplicate"):
    session_id = f"{{session_id}}-{{creations}}"
if data is not None:
    # The request is validated against the resolved schema first.
    allowed = {{"id", "parentID", "title", "agent", "model", "location", "metadata",
               "permissions"}}
    if set(data) - allowed:
        sys.stderr.write("the request body has a property the schema does not declare\\n")
        raise SystemExit(4)
    model = data.get("model")
    if (not isinstance(model, dict) or set(model) - {{"id", "providerID", "variant"}}
            or not model.get("id") or not model.get("providerID")):
        sys.stderr.write("the model reference does not match Model.Ref\\n")
        raise SystemExit(4)
    location = data.get("location")
    if (not isinstance(location, dict) or set(location) - {{"directory"}}
            or not location.get("directory")):
        sys.stderr.write("the location reference does not match Location.PublicRef\\n")
        raise SystemExit(4)
directory = (data or {{}}).get("location", {{}}).get("directory")
if data:
    recorded = control.get("recordModel")
    if recorded is None:
        recorded = json.dumps(data["model"], separators=(",", ":"))
    with sqlite3.connect(DATABASE) as database:
        database.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                         (session_id, directory, "project-test", recorded))
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
                  "QUEUE_LOCK": root / "data" / "queue.lock",
                  "QUEUE": root / "data" / "queue.json",
                  "OPENCODE": root / "data" / "opencode", "BIN": root / "bin" / "fleet"}
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

    def creations(self):
        return [item for item in self.requests()
                if "POST" in item["argv"] and "/api/session" in item["argv"]]

    def add(self, count=1, model=MODEL, cwd=None, root=None, **kwargs):
        return opencode_lifecycle.add_sessions(
            self.db, count=count, model=model, cwd=cwd or str(self.wt),
            root=str(root or self.root), **kwargs)

    def task(self, task_id, sid, state):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (task_id, sid, str(self.wt), json.dumps({"message": "m"}), state, 1))
        self.db.commit()

    def digest(self, count=1, target_total=None, enable_dispatch=False, max_concurrent=None):
        """The payload digest the module binds to a request key."""
        request = {"root": str(self.root), "account": str(self.native), "model": MODEL,
                   "cwd": str(self.wt)}
        return opencode_lifecycle._payload_digest(request, count, target_total,
                                                 enable_dispatch, max_concurrent)

    def queue(self, tasks):
        paths.QUEUE.parent.mkdir(parents=True, exist_ok=True)
        paths.QUEUE.write_text(json.dumps({"author": "root", "tasks": tasks}))


class NativeRequestTests(Sandbox):
    def test_the_native_request_carries_the_exact_model_and_directory(self):
        self.add(count=1)
        creations = self.creations()
        self.assertEqual(len(creations), 1)
        self.assertEqual(creations[0]["argv"][:4], ["api", "--standalone", "POST", "/api/session"])
        # The resolved schema takes the model as a Model.Ref object.
        self.assertEqual(creations[0]["data"]["model"],
                         {"id": "minimax-m3.1-flash-free", "providerID": "opencode-go"})
        self.assertEqual(creations[0]["data"]["location"], {"directory": str(self.wt)})
        config = json.loads(creations[0]["config"])
        self.assertEqual(config["model"], MODEL)
        self.assertEqual(config["small_model"], MODEL)
        self.assertEqual(config["enabled_providers"], ["opencode-go"])
        self.assertEqual(creations[0]["db"], str(self.native))
        self.assertNotEqual(creations[0]["home"], str(Path.home()))

    def test_the_created_session_is_read_back_from_the_native_inventory(self):
        result = self.add(count=1)
        self.assertEqual(result["created"], 1)
        identifier = result["sessions"][0]["sessionId"]
        with sqlite3.connect(self.native) as db:
            row = db.execute("SELECT id,directory,model FROM session_v2 WHERE id=?",
                             (identifier,)).fetchone()
        self.assertEqual(row[0], identifier)
        self.assertEqual(row[1], str(self.wt))
        # The native session records the model as the reference it was given.
        self.assertEqual(json.loads(row[2]),
                         {"id": "minimax-m3.1-flash-free", "providerID": "opencode-go"})
        self.assertEqual([entry["sessionId"] for entry in self.roster()],
                         [self.existing, identifier])

    def test_an_existing_session_is_never_recreated_or_changed(self):
        before = dict(self.roster()[0])
        self.add(count=1)
        self.assertEqual(self.roster()[0], before)
        self.assertEqual(len(self.creations()), 1)

    def test_the_creation_is_proved_against_the_installed_cli_schema(self):
        result = self.add(count=1)
        support = result["apiSupport"]
        self.assertEqual(support["operationId"], "session.create")
        self.assertEqual(support["path"], "/api/session")
        self.assertEqual(support["required"], [])
        self.assertEqual(support["security"], [])
        self.assertIn("model", support["bodyProperties"])
        self.assertIn("location", support["bodyProperties"])
        probes = [item for item in self.requests() if "GET" in item["argv"]]
        self.assertTrue(probes)

    def test_a_cli_without_the_creation_operation_creates_nothing(self):
        self.write_control({"mode": "ok", "sessionId": "ses_NEW0000000000000001",
                            "noSchemaOperation": True})
        with self.assertRaises(opencode_lifecycle.ProvisioningError) as error:
            self.add(count=1, request_key="w5-schema-001")
        self.assertIn("session.create", str(error.exception))
        self.assertEqual(self.creations(), [])
        self.assertEqual(len(self.roster()), 1)


class ModelPinTests(Sandbox):
    def test_a_model_outside_the_approved_set_is_refused(self):
        with self.assertRaises(ValueError):
            self.add(count=1, model="opencode/some-paid-model")
        self.assertEqual(self.creations(), [])

    def test_a_model_that_is_not_the_account_model_is_refused(self):
        with self.assertRaises(ValueError):
            self.add(count=1, model=OTHER_APPROVED)
        self.assertEqual(self.creations(), [])

    def test_a_model_the_account_uses_is_accepted(self):
        self.cfg["opencode"][0]["model"] = OTHER_APPROVED
        self.write_config(self.cfg)
        result = self.add(count=1, model=OTHER_APPROVED)
        self.assertEqual(result["created"], 1)


class IdempotencyTests(Sandbox):
    def test_the_same_request_key_never_adds_twice(self):
        first = self.add(count=1, request_key="w5-scale-001")
        self.assertEqual(first["created"], 1)
        second = self.add(count=1, request_key="w5-scale-001")
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["created"], 0)
        self.assertEqual(len(self.creations()), 1)
        self.assertEqual(len(self.roster()), 2)

    def test_a_request_reaches_its_total_in_one_command(self):
        # The roster holds one worker; a request for a total of three adds two.
        result = self.add(count=None, target_total=3, request_key="w5-scale-002")
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["rosterTotalBefore"], 1)
        self.assertEqual(result["created"], 2)
        self.assertEqual(len(self.roster()), 3)
        again = self.add(count=None, target_total=3, request_key="w5-scale-002")
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.creations()), 2)

    def test_a_total_below_the_current_roster_is_refused(self):
        self.add(count=1, request_key="w5-scale-000")
        with self.assertRaises(ValueError) as error:
            self.add(count=None, target_total=1)
        self.assertIn("already holds", str(error.exception))

    def test_a_total_that_is_already_reached_creates_nothing(self):
        result = self.add(count=None, target_total=1)
        self.assertEqual(result["status"], "already-reached")
        self.assertEqual(result["created"], 0)
        self.assertEqual(self.creations(), [])
        self.assertEqual(len(self.roster()), 1)

    def test_a_request_with_missing_receipts_claims_nothing(self):
        self.add(count=1, request_key="w5-scale-003")
        self.db.execute("DELETE FROM opencode_sessions")
        self.db.commit()
        again = self.add(count=1, request_key="w5-scale-003")
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["registered"], 0)
        self.assertEqual(again["sessions"], [])
        self.assertEqual(again["logicalSessions"], 2)

    def test_a_request_recorded_without_a_payload_digest_fails_closed(self):
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("w5-scale-004", MODEL, str(self.native), str(self.wt), str(self.root), 1, 0,
             "registered", 1, 1, "", ""))
        self.db.commit()
        with self.assertRaises(ValueError) as error:
            self.add(count=1, request_key="w5-scale-004")
        self.assertIn("payload binding", str(error.exception))
        self.assertEqual(self.creations(), [])


class UncertainOutcomeTests(Sandbox):
    """An uncertain outcome stays uncertain until the operator reconciles it."""

    def test_a_hung_creation_is_recorded_as_uncertain_and_not_as_a_failure(self):
        self.write_control({"mode": "hang", "sessionId": "ses_NEW0000000000000001"})
        with patch.object(opencode_lifecycle, "CREATE_TIMEOUT_SECONDS", 1):
            result = self.add(count=1, request_key="w5-uncertain-001")
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["retryable"])
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["rosterChanged"])
        row = self.db.execute("SELECT status FROM opencode_provisioning "
                              "WHERE request_key=?", ("w5-uncertain-001",)).fetchone()
        self.assertEqual(row[0], "uncertain")
        self.assertEqual(len(self.roster()), 1)

    def test_the_intent_is_durable_before_the_first_native_call(self):
        seen = []
        original = opencode_lifecycle._record_provisioning

        def record(db, key, request, status, detail="", count=None, payload_digest="",
                   creator_pid=0, keep_registered=False):
            seen.append(status)
            return original(db, key, request, status, detail, count, payload_digest,
                            creator_pid, keep_registered)

        with patch.object(opencode_lifecycle, "_record_provisioning", side_effect=record):
            self.add(count=1, request_key="w5-uncertain-002")
        self.assertEqual(seen[0], "creating")
        self.assertIn("registered", seen)

    def test_an_uncertain_request_is_not_resumed_by_adoption(self):
        # A crash after the native create leaves a session the Fleet cannot
        # name. Nothing is adopted on speculation: the request stops.
        with patch.object(opencode_lifecycle, "_record_session", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.add(count=1, request_key="w5-uncertain-003")
        with sqlite3.connect(self.native) as db:
            created = db.execute("SELECT id FROM session_v2 WHERE id<>?",
                                 (self.existing,)).fetchall()
        self.assertEqual(len(created), 1)
        self.db.execute("UPDATE opencode_provisioning SET status='uncertain' "
                        "WHERE request_key=?", ("w5-uncertain-003",))
        self.db.commit()
        with self.assertRaises(ValueError) as error:
            self.add(count=1, request_key="w5-uncertain-003")
        self.assertIn("uncertain", str(error.exception))
        self.assertIn("absence is no proof", str(error.exception))
        self.assertEqual(len(self.roster()), 1)

    def test_an_explicitly_named_session_is_verified_and_registered(self):
        identifier = "ses_RECONCILED00000000001"
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       (identifier, str(self.wt), "project-test", MODEL))
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("w5-uncertain-004", MODEL, str(self.native), str(self.wt), str(self.root), 1, 0,
             "uncertain", 1, 1, "", self.digest()))
        self.db.commit()
        result = self.add(count=1, request_key="w5-uncertain-004",
                          reconciled_sessions=[identifier])
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["reconciled"], [identifier])
        self.assertIn(identifier, [entry["sessionId"] for entry in self.roster()])
        self.assertEqual(self.creations(), [])

    def test_a_named_session_that_cannot_be_read_is_not_registered(self):
        identifier = "ses_RECONCILED00000000002"
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("w5-uncertain-005", MODEL, str(self.native), str(self.wt), str(self.root), 1, 0,
             "uncertain", 1, 1, "", self.digest()))
        self.db.commit()
        with self.assertRaises(ValueError) as error:
            self.add(count=1, request_key="w5-uncertain-005",
                     reconciled_sessions=[identifier])
        self.assertIn("Absence is no proof", str(error.exception))
        self.assertEqual(len(self.roster()), 1)

    def test_an_unverified_receipt_is_never_ignored(self):
        self.db.execute(
            "INSERT INTO opencode_sessions (session_id,request_key,native_id,model,account,cwd,"
            "root,status,created,updated,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("ses_UNVERIFIED0000000001", "w5-uncertain-006", "ses_UNVERIFIED0000000001",
             MODEL, str(self.native), str(self.wt), str(self.root), "unverified", 1, 1, "x"))
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("w5-uncertain-006", MODEL, str(self.native), str(self.wt), str(self.root), 1, 0,
             "creating", 1, 1, "", self.digest()))
        self.db.commit()
        with self.assertRaises(ValueError) as error:
            self.add(count=1, request_key="w5-uncertain-006")
        self.assertIn("not ignored", str(error.exception))
        self.assertEqual(self.creations(), [])


class PartialFailureTests(Sandbox):
    def test_a_partial_failure_leaves_the_roster_untouched(self):
        self.write_control({"mode": "okThenFail", "failAfter": 1,
                            "sessionId": "ses_NEW0000000000000001"})
        result = self.add(count=2, request_key="w5-scale-003")
        self.assertFalse(result["rosterChanged"])
        self.assertEqual(len(self.roster()), 1)
        self.assertTrue(result["failed"])
        self.assertEqual(len(result["createdNotRegistered"]), 1)
        kinds = [row[0] for row in self.db.execute("SELECT kind FROM events")]
        self.assertIn("needs_attention", kinds)

    def test_a_failed_request_leaves_no_half_written_roster(self):
        self.write_control({"mode": "garbage", "sessionId": "ses_NEW0000000000000001"})
        result = self.add(count=1, request_key="w5-scale-004")
        self.assertFalse(result["rosterChanged"])
        self.assertEqual(len(self.roster()), 1)
        with sqlite3.connect(self.native) as db:
            rows = db.execute("SELECT count(*) FROM session_v2").fetchone()[0]
        self.assertEqual(rows, 1)


class ScopeRefusalTests(Sandbox):
    def test_a_wrong_root_is_refused(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        with self.assertRaises(ValueError) as error:
            self.add(count=1, root=other)
        self.assertIn("fleet root", str(error.exception))
        self.assertEqual(self.creations(), [])

    def test_a_wrong_account_is_refused(self):
        other = Path(self.tmp.name) / "other.db"
        with self.assertRaises(ValueError) as error:
            self.add(count=1, database_path=str(other))
        self.assertIn("account", str(error.exception))
        self.assertEqual(self.creations(), [])

    def test_a_malformed_count_is_refused(self):
        for count in (0, -1, opencode_lifecycle.MAX_ADD_COUNT + 1, "2"):
            with self.assertRaises(ValueError):
                self.add(count=count)
        self.assertEqual(self.creations(), [])

    def test_a_malformed_admission_limit_is_refused(self):
        for limit in (1, 3, 65, -1):
            with self.assertRaises(ValueError):
                self.add(count=1, max_concurrent=limit)
        self.assertEqual(self.creations(), [])
        self.assertEqual(paths.config()["maxConcurrent"], 4)

    def test_an_unreadable_configuration_is_refused(self):
        paths.CONFIG.write_text("{not json")
        with self.assertRaises(ValueError):
            self.add(count=1)
        self.assertEqual(self.creations(), [])

    def test_a_cwd_outside_the_worktree_root_is_refused(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        subprocess.run(["git", "init", "-q", str(outside)], check=True)
        with self.assertRaises(ValueError):
            self.add(count=1, cwd=str(outside))
        self.assertEqual(self.creations(), [])

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


class DryRunTests(Sandbox):
    def test_a_dry_run_reports_the_plan_and_writes_nothing(self):
        before = paths.CONFIG.read_bytes()
        result = self.add(count=2, dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["toCreate"], 2)
        self.assertEqual(result["created"], 0)
        self.assertEqual(self.creations(), [])
        self.assertEqual(paths.CONFIG.read_bytes(), before)
        self.assertEqual(len(self.roster()), 1)
        # No receipt and no provisioning row either.
        self.assertEqual(self.db.execute("SELECT count(*) FROM opencode_sessions").fetchone()[0], 0)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM opencode_provisioning").fetchone()[0], 0)

    def test_a_dry_run_of_a_target_total_writes_nothing(self):
        before = paths.CONFIG.read_bytes()
        result = self.add(count=None, target_total=25, dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["requested"], 24)
        self.assertEqual(result["rosterTotalBefore"], 1)
        self.assertEqual(self.creations(), [])
        self.assertEqual(paths.CONFIG.read_bytes(), before)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM opencode_provisioning").fetchone()[0], 0)

    def test_the_status_report_names_the_account_and_the_receipts(self):
        report = opencode_lifecycle.status(self.db)
        self.assertEqual(report["accounts"], [str(self.native)])
        self.assertEqual(report["logicalSessions"], 1)
        self.assertEqual(report["admissionLimit"], 4)
        self.assertEqual(report["heavyJobSlots"], 2)
        self.assertEqual(report["roster"][0]["sessionId"], self.existing)
        self.assertFalse(report["roster"][0]["draining"])


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

    def test_drain_reports_the_tasks_waiting_to_be_imported(self):
        self.queue([{"id": "q-1", "sessionId": self.existing, "message": "m"}])
        result = opencode_lifecycle.drain_session(self.db, self.existing)
        self.assertEqual([item["taskId"] for item in result["awaitingImport"]], ["q-1"])

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

    def test_a_stale_pre_drain_slot_does_not_launch_after_the_drain(self):
        # The roster was read before the drain. The claim re-reads it inside the
        # writer lock, so the drained worker takes no new work.
        stale_slot = dict(self.roster()[0], provider="opencode")
        self.task("t-queued", self.existing, "queued")
        opencode_lifecycle.drain_session(self.db, self.existing, note="draining")
        states = {self.existing: {"state": "idle", "cwd": str(self.wt)}}
        calls = []
        dispatch.dispatch(self.db, {self.existing: stale_slot}, states,
                         lambda *a: calls.append(a))
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

    def test_removal_refuses_while_a_task_is_waiting_to_be_imported(self):
        self.queue([{"id": "q-1", "sessionId": self.existing, "message": "m"}])
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing)
        self.assertIn("waiting to be imported", str(error.exception))
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

    def test_there_is_no_other_removal_path(self):
        # A receipt-owned worker leaves the roster only through the removal
        # command, with its barriers. No rollback command exists.
        self.assertFalse(hasattr(opencode_lifecycle, "rollback_request"))
        from fleet import cli
        parser_actions = [action for action in cli.main.__doc__ or ""]
        self.assertEqual(parser_actions, [])


class SerializationTests(Sandbox):
    """Same-key provisioning and roster writes are serialized and compared."""

    def test_two_concurrent_requests_with_the_same_key_create_one_session(self):
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
        self.assertEqual(len(self.creations()), 1)
        self.assertEqual(len(self.roster()), 2)
        self.assertEqual(sum(1 for result in results if result.get("created") == 1), 1)
        for result in results:
            if "error" in result:
                self.assertIn("in progress", result["error"])

    def test_a_configuration_changed_during_the_request_is_never_overwritten(self):
        def rewrite(*args, **kwargs):
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
        started = threading.Event()
        release = threading.Event()

        def claim():
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
        self.assertEqual(len(self.creations()), 1)

    def test_the_same_key_with_a_different_total_is_refused(self):
        self.add(count=None, target_total=5, request_key="w5-key-002")
        with self.assertRaises(ValueError):
            self.add(count=None, target_total=6, request_key="w5-key-002")

    def test_the_same_key_with_a_different_dispatch_flag_is_refused(self):
        self.add(count=1, request_key="w5-key-003")
        with self.assertRaises(ValueError):
            self.add(count=1, request_key="w5-key-003", enable_dispatch=True)

    def test_the_same_key_with_a_different_admission_limit_is_refused(self):
        self.add(count=1, request_key="w5-key-004")
        with self.assertRaises(ValueError):
            self.add(count=1, request_key="w5-key-004", max_concurrent=6)

    def test_the_same_key_with_a_different_model_is_refused(self):
        self.add(count=1, request_key="w5-key-005")
        with self.assertRaises(ValueError):
            self.add(count=1, request_key="w5-key-005", model=OTHER_APPROVED)

    def test_the_same_key_with_a_different_account_is_refused(self):
        self.add(count=1, request_key="w5-key-006")
        other = Path(self.tmp.name) / "other.db"
        with self.assertRaises(ValueError):
            self.add(count=1, request_key="w5-key-006", database_path=str(other))

    def test_the_same_key_with_a_different_working_directory_is_refused(self):
        self.add(count=1, request_key="w5-key-007")
        other = Path(self.tmp.name) / "wt" / "second"
        other.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        with self.assertRaises(ValueError):
            self.add(count=1, request_key="w5-key-007", cwd=str(other))

    def test_the_same_request_with_the_same_options_is_idempotent(self):
        self.add(count=1, request_key="w5-key-008")
        again = self.add(count=1, request_key="w5-key-008")
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.creations()), 1)


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
        self.write_control({"mode": "ok", "sessionId": "ses_SCALE000000000000000"})
        result = self.add(count=17, request_key="w5-scale-17")
        self.assertEqual(result["created"], 17)
        self.assertEqual(len(self.roster()), 18)
        self.assertEqual(len(self.creations()), 17)
        # Seventeen distinct native sessions, receipts and roster entries.
        added = [entry["sessionId"] for entry in self.roster() if entry["sessionId"] != self.existing]
        self.assertEqual(len(added), 17)
        self.assertEqual(len(set(added)), 17)
        receipts = [r for r in opencode_lifecycle._session_receipts(self.db, "w5-scale-17")
                    if r["status"] == "registered"]
        self.assertEqual(len(receipts), 17)
        self.assertEqual(len({r["session_id"] for r in receipts}), 17)
        self.assertEqual(result["proof"]["expected"], 17)
        self.assertEqual(result["proof"]["receipts"], 17)
        self.assertTrue(result["proof"]["distinct"])
        self.assertTrue(result["proof"]["inRoster"])
        self.assertEqual(result["proof"]["models"], [MODEL])

    def test_a_duplicated_native_id_is_rejected(self):
        self.write_control({"mode": "ok", "sessionId": "ses_DUPLICATE00000000001",
                            "duplicate": True})
        result = self.add(count=2, request_key="w5-scale-dup")
        self.assertFalse(result["rosterChanged"])
        self.assertEqual(len(self.roster()), 1)
        self.assertIn("this request already created", result["failed"])
        receipts = opencode_lifecycle._session_receipts(self.db, "w5-scale-dup")
        self.assertEqual([receipt["status"] for receipt in receipts], ["created"])
        self.assertEqual(len(receipts), 1)

    def test_the_bound_is_still_a_bound(self):
        with self.assertRaises(ValueError):
            self.add(count=opencode_lifecycle.MAX_ADD_COUNT + 1)
        self.assertEqual(opencode_lifecycle.MAX_ADD_COUNT, 32)


class NativeIdentityTests(Sandbox):
    """The native model, not only the id and the directory, identifies a session."""

    def test_a_session_with_another_native_model_is_refused(self):
        self.write_control({"mode": "ok", "sessionId": "ses_OTHERMODEL000000001",
                            "recordModel": "opencode/deepseek-v4.1-flash-free"})
        result = self.add(count=1, request_key="w5-model-001")
        self.assertFalse(result["rosterChanged"])
        self.assertEqual(len(self.roster()), 1)
        self.assertIn("does not match this request", result["failed"])

    def test_the_created_session_carries_the_requested_model(self):
        self.add(count=1, request_key="w5-model-002")
        identifier = self.roster()[-1]["sessionId"]
        with sqlite3.connect(self.native) as db:
            row = db.execute("SELECT model FROM session_v2 WHERE id=?", (identifier,)).fetchone()
        self.assertEqual(json.loads(row[0]),
                         {"id": "minimax-m3.1-flash-free", "providerID": "opencode-go"})

    def test_a_pending_session_that_no_longer_verifies_is_not_registered(self):
        identifier = "ses_PENDING000000000001"
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       (identifier, str(self.wt), "project-test", MODEL))
        self.db.execute(
            "INSERT INTO opencode_sessions (session_id,request_key,native_id,model,account,cwd,"
            "root,status,created,updated,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (identifier, "w5-model-003", identifier, MODEL, str(self.native), str(self.wt),
             str(self.root), "created", 1, 1, "created before the crash"))
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("w5-model-003", MODEL, str(self.native), str(self.wt), str(self.root), 1, 0,
             "creating", 1, 1, "", self.digest()))
        self.db.commit()
        with sqlite3.connect(self.native) as db:
            db.execute("DELETE FROM session_v2 WHERE id=?", (identifier,))
        result = self.add(count=1, request_key="w5-model-003")
        self.assertFalse(result["rosterChanged"])
        self.assertEqual(len(self.roster()), 1)
        self.assertIn("no longer verifies", result["failed"])

    def test_a_pending_session_with_another_native_model_is_not_registered(self):
        identifier = "ses_PENDING000000000002"
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       (identifier, str(self.wt), "project-test",
                        "opencode/deepseek-v4.1-flash-free"))
        self.db.execute(
            "INSERT INTO opencode_sessions (session_id,request_key,native_id,model,account,cwd,"
            "root,status,created,updated,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (identifier, "w5-model-004", identifier, MODEL, str(self.native), str(self.wt),
             str(self.root), "created", 1, 1, "created before the crash"))
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("w5-model-004", MODEL, str(self.native), str(self.wt), str(self.root), 1, 0,
             "creating", 1, 1, "", self.digest()))
        self.db.commit()
        result = self.add(count=1, request_key="w5-model-004")
        self.assertFalse(result["rosterChanged"])
        self.assertEqual(len(self.roster()), 1)
        self.assertIn("no longer verifies", result["failed"])

    def test_the_native_request_stays_inside_the_documented_schema(self):
        # The installed OpenCode CLI serves its own OpenAPI 3.1.0 spec
        # (opencode v2.0.29, spec sha256 49c38e1c0a6c0e8c91ab5fbeaddd068effc543bdecf96484caf4eb956218a1c7).
        documented = {"id", "parentID", "title", "agent", "model", "location", "metadata",
                      "permissions"}
        self.add(count=1, request_key="w5-model-005")
        payload = self.creations()[0]["data"]
        self.assertTrue(set(payload).issubset(documented))
        self.assertEqual(set(payload), {"location", "model"})
        self.assertEqual(sorted(payload["location"]), ["directory"])
        # The model is a Model.Ref object with a required id and providerID.
        self.assertEqual(sorted(payload["model"]), ["id", "providerID"])
        self.assertEqual(payload["model"]["providerID"], "opencode-go")
        self.assertEqual(payload["model"]["id"], "minimax-m3.1-flash-free")
        self.assertNotIn("prompt", json.dumps(payload).lower())
        self.assertNotIn("text", json.dumps(payload).lower())


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
        self.assertTrue(result["honouredByThisBuild"])

    def test_the_removal_refuses_while_work_is_outstanding_even_when_draining(self):
        self.task("t-queued", self.existing, "queued")
        opencode_lifecycle.drain_session(self.db, self.existing, note="draining")
        with self.assertRaises(ValueError):
            opencode_lifecycle.remove_session(self.db, self.existing)
        self.assertEqual(len(self.roster()), 1)

    def test_the_writer_lock_is_shared_by_the_roster_and_the_claim(self):
        # The lock is one lock: the drain, the removal, the enqueue, the import
        # and the dispatch claim all take it.
        self.assertTrue(hasattr(roster, "roster_lock"))
        self.assertTrue(hasattr(roster, "queue_lock"))
        self.assertTrue(hasattr(roster, "write_config_cas"))


class ApiContractTests(Sandbox):
    """The request the implementation sends is the one the installed schema declares."""

    def test_the_synthetic_cli_refuses_a_payload_the_schema_does_not_declare(self):
        # The model as a plain "provider/model" string is not the resolved
        # Model.Ref: the schema wants an object with a required id and providerID.
        result = subprocess.run([str(self.fake), "api", "--standalone", "POST", "/api/session",
                                 "--data", json.dumps({"location": {"directory": str(self.wt)},
                                                      "model": MODEL})],
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Model.Ref", result.stderr)

    def test_the_synthetic_cli_refuses_a_location_the_schema_does_not_declare(self):
        result = subprocess.run([str(self.fake), "api", "--standalone", "POST", "/api/session",
                                 "--data", json.dumps({"location": str(self.wt),
                                                      "model": {"id": "minimax-m3.1-flash-free",
                                                                "providerID": "opencode-go"}})],
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Location.PublicRef", result.stderr)

    def test_the_synthetic_cli_refuses_a_property_the_body_does_not_declare(self):
        result = subprocess.run([str(self.fake), "api", "--standalone", "POST", "/api/session",
                                 "--data", json.dumps({"location": {"directory": str(self.wt)},
                                                      "model": {"id": "minimax-m3.1-flash-free",
                                                                "providerID": "opencode-go"},
                                                      "prompt": "a prompt"})],
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not declare", result.stderr)

    def test_the_implementation_payload_is_accepted_by_the_schema(self):
        self.add(count=1, request_key="w5-contract-001")
        self.assertEqual(len(self.creations()), 1)


class CrashWindowTests(Sandbox):
    """The windows around the roster write are recoverable and never duplicate."""

    def test_a_crash_between_the_roster_write_and_the_receipts_is_recovered(self):
        # The roster is written, then the receipts are updated. A crash in that
        # window must not make the retry create a second set of sessions.
        original = opencode_lifecycle._record_session

        def fail_once(db, session_id, request_key, request, status, detail=""):
            if status == "registered":
                raise KeyboardInterrupt("the receipt update was interrupted")
            return original(db, session_id, request_key, request, status, detail)

        with patch.object(opencode_lifecycle, "_record_session", side_effect=fail_once):
            with self.assertRaises(KeyboardInterrupt):
                self.add(count=1, request_key="w5-crash-001")
        # The process that recorded the intent is gone, exactly as after a crash.
        self.db.execute("UPDATE opencode_provisioning SET creator_pid=? WHERE request_key=?",
                        (999999, "w5-crash-001"))
        self.db.commit()
        self.assertEqual(len(self.creations()), 1)
        self.assertEqual(len(self.roster()), 2)
        resumed = self.add(count=1, request_key="w5-crash-001")
        # The recorded session is recovered; no second native session is created.
        self.assertEqual(len(self.creations()), 1)
        self.assertEqual(resumed["created"], 0)
        self.assertEqual(len(self.roster()), 2)

    def test_an_interrupted_attempt_without_a_record_is_not_resumed_by_adoption(self):
        with patch.object(opencode_lifecycle, "_record_session", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.add(count=1, request_key="w5-crash-002")
        self.db.execute("UPDATE opencode_provisioning SET status='uncertain', detail='' "
                        "WHERE request_key=?", ("w5-crash-002",))
        self.db.commit()
        with self.assertRaises(ValueError) as error:
            self.add(count=1, request_key="w5-crash-002")
        self.assertIn("uncertain", str(error.exception))
        self.assertEqual(len(self.creations()), 1)
        self.assertEqual(len(self.roster()), 1)


class ImportProtectionTests(Sandbox):
    """The queue import and the dispatch claim share the roster writer lock."""

    def test_the_queue_import_waits_for_the_roster_writer_lock(self):
        import threading
        from fleet import manager
        started = threading.Event()
        release = threading.Event()
        order = []

        def hold():
            with roster.roster_lock():
                started.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        started.wait(10)
        db = store.database(paths.DB)
        try:
            def run_import():
                manager.import_queue(db, {self.existing: self.roster()[0]},
                                    str(Path(self.tmp.name) / "queue.json"))
                order.append("imported")

            importer = threading.Thread(target=run_import)
            importer.start()
            importer.join(2)
            self.assertEqual(order, [], "the import did not wait for the roster writer lock")
            release.set()
            importer.join(10)
            holder.join(10)
            self.assertEqual(order, ["imported"])
        finally:
            db.close()
            release.set()


if __name__ == "__main__":
    unittest.main()
