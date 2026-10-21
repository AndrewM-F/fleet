"""Independent qualification of the Fleet OpenCode session lifecycle.

This suite qualifies the lifecycle invariants from the outside. It is written
against the published source head, it shares no fixture with the source owner's
own suite, and it owns exactly one file: this one.

Every fixture is synthetic and disposable:

* the fleet root is a temporary directory, so ``paths.ROOT`` and every path
  derived from it point at a sandbox;
* the native identity inventory is a temporary SQLite file with the shape the
  runtime reads (``session_v2``);
* the native session server is a fake executable that serves the schema the
  Fleet consults and records the sessions it is asked to create;
* the git worktree is a temporary repository, so a dirty file, an untracked file
  and an unpublished commit are real but local.

Nothing here contacts a provider, writes a real native database, starts a live
manager, touches a live fleet, mutates a registered worktree, or removes
anything from the host. No production fix is implemented or copied: where the
published source does not satisfy an invariant, the test fails and the failure
is the acceptance evidence that is routed back to the source owner.
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

from fleet import (manager, model_selection, opencode,  # noqa: E402
                   opencode_lifecycle, paths, recovery, roster, store)

MODEL = "opencode-go/minimax-m3.1-flash-free"
OTHER_APPROVED = "opencode/minimax-m3.1-flash-free"

# A process id that cannot exist on this host, so the runtime's liveness check
# reports it as gone. It models an attempt whose process died, without touching
# the check itself.
DEAD_PID = 4194303

# The schema the runtime proves a session creation against: the model is a
# Model.Ref object with a required id and providerID, the location is a
# Location.PublicRef object with a required directory, the body allows no other
# property, and the operation declares no security scheme.
NATIVE_SCHEMA = {
    "openapi": "3.1.0",
    "info": {"title": "opencode HttpApi", "version": "0.0.1"},
    "components": {"schemas": {
        "Model.Ref": {"type": "object",
                      "properties": {"id": {"type": "string"},
                                     "providerID": {"type": "string"}},
                      "required": ["id", "providerID"], "additionalProperties": False},
        "Location.PublicRef": {"type": "object",
                                "properties": {"directory": {"type": "string"}},
                                "required": ["directory"], "additionalProperties": False},
    }},
    "paths": {
        "/api/session": {
            "post": {
                "operationId": "session.create",
                "summary": "Create session",
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "model": {"$ref": "#/components/schemas/Model.Ref"},
                        "location": {"$ref": "#/components/schemas/Location.PublicRef"},
                    },
                    "required": [],
                    "additionalProperties": False,
                }}}},
                "responses": {"200": {"description": "Success"}},
                "security": [],
            },
        },
    },
}

FAKE_NATIVE_CLI = '''#!/usr/bin/env python3
"""A fake native session server for the independent qualification.

It serves the schema the runtime consults, records every call, and writes the
sessions it creates into the synthetic identity database. It contacts no
provider and reads no real native state.
"""
import json
import os
import sqlite3
import sys
import time

CONTROL = {control!r}
DATABASE = {database!r}
LOG = {log!r}
SERVED = {served!r}
SCHEMA = {schema!r}

control = json.load(open(CONTROL))
argv = sys.argv[1:]
payload = None
for index, value in enumerate(argv):
    if value == "--data":
        payload = json.loads(argv[index + 1])
with open(LOG, "a") as stream:
    stream.write(json.dumps({{"argv": argv, "payload": payload}}) + "\\n")

if "GET" in argv and "/openapi.json" in argv:
    if control.get("mode") == "refuseSchema":
        print(json.dumps({{"openapi": "3.1.0", "paths": {{}}}}))
        raise SystemExit(0)
    with open(SERVED, "w") as stream:
        stream.write(SCHEMA)
    print(SCHEMA)
    raise SystemExit(0)

mode = control.get("mode")
if mode == "hang":
    time.sleep(600)

with sqlite3.connect(DATABASE) as database:
    rows = database.execute("SELECT count(*) FROM session_v2").fetchone()[0]
identifier = control.get("fixedId") or (control["idPrefix"] + "-" + str(rows + 1))
if mode == "duplicate":
    identifier = control["idPrefix"] + "-1"
directory = (payload or {{}}).get("location", {{}}).get("directory")
if payload is not None:
    model = payload.get("model")
    if isinstance(model, str) and "/" in model:
        provider, _, name = model.partition("/")
        model = {{"id": name, "providerID": provider}}
    with sqlite3.connect(DATABASE) as database:
        database.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                         (identifier, directory, "project-independent",
                          json.dumps(model, separators=(",", ":"))))
if mode == "refuseCreate":
    sys.stderr.write("fake native server refused the request\\n")
    raise SystemExit(3)
print(json.dumps({{"data": {{"id": identifier, "directory": directory,
                           "model": (payload or {{}}).get("model")}}}}))
'''


class QualificationSandbox(unittest.TestCase):
    """A temporary fleet root with a synthetic native inventory and a fake CLI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # A real but local git worktree: a committed file, a dirty tracked file,
        # an untracked file and an unpublished commit.
        self.worktree = self.root / "worktrees" / "lane"
        self.worktree.mkdir(parents=True)
        self.git("init", "-q", str(self.worktree))
        self.git("-C", str(self.worktree), "config", "user.email", "w8@example.invalid")
        self.git("-C", str(self.worktree), "config", "user.name", "W8 Qualification")
        (self.worktree / "tracked.txt").write_text("original\n")
        self.git("-C", str(self.worktree), "add", "tracked.txt")
        self.git("-C", str(self.worktree), "commit", "-q", "-m", "base")
        (self.worktree / "tracked.txt").write_text("changed by the lane\n")
        (self.worktree / "untracked.txt").write_text("scratch\n")
        (self.worktree / "second.txt").write_text("unpublished\n")
        self.git("-C", str(self.worktree), "add", "second.txt")
        self.git("-C", str(self.worktree), "commit", "-q", "-m", "unpublished work")

        # The synthetic native identity inventory.
        self.native = self.root / "native" / "opencode.db"
        self.native.parent.mkdir()
        with sqlite3.connect(self.native) as db:
            db.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, project_id TEXT, "
                       "model TEXT)")
        self.existing = "ses_EXISTING000000000001"
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT INTO session_v2 VALUES (?,?,?,?)",
                       (self.existing, str(self.worktree), "project-independent",
                        json.dumps({"id": "minimax-m3.1-flash-free",
                                    "providerID": "opencode-go"})))

        self.control = self.root / "control.json"
        self.log = self.root / "fake-native.log"
        self.served = self.root / "served-schema.json"
        self.write_control({"mode": "ok", "idPrefix": "ses_NEW000000000000000"})
        self.fake = self.root / "fake-native"
        self.fake.write_text(FAKE_NATIVE_CLI.format(control=str(self.control),
                                                    database=str(self.native),
                                                    log=str(self.log),
                                                    served=str(self.served),
                                                    schema=json.dumps(NATIVE_SCHEMA)))
        self.fake.chmod(0o755)

        self.config = {
            "worktreeRoot": str(self.root / "worktrees"),
            "executables": {"opencode": str(self.fake)},
            "maxConcurrent": 4,
            "pi": [], "claude": [],
            "opencode": [{"sessionId": self.existing, "cwd": str(self.worktree),
                          "sessionCwd": str(self.worktree), "databasePath": str(self.native),
                          "model": MODEL, "provider": "opencode", "dispatchEnabled": True}],
        }
        (self.root / "config").mkdir()
        (self.root / "data").mkdir()
        (self.root / "config" / "fleet.local.json").write_text(json.dumps(self.config))
        overrides = {"ROOT": self.root, "CONFIG": self.root / "config" / "fleet.local.json",
                     "DATA": self.root / "data", "LOGS": self.root / "logs",
                     "DB": self.root / "data" / "fleet.sqlite",
                     "STATUS": self.root / "data" / "status.json",
                     "QUEUE": self.root / "data" / "queue.json",
                     "QUEUE_LOCK": self.root / "data" / "queue.lock",
                     "MANAGER_LOCK": self.root / "data" / "manager.lock",
                     "OPENCODE": self.root / "data" / "opencode",
                     "BIN": self.root / "bin" / "fleet"}
        for name, value in overrides.items():
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.db = store.database(paths.DB)
        self.addCleanup(self.db.close)

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def git(*args):
        subprocess.run(["git", *args], check=True, capture_output=True)

    def write_control(self, control):
        self.control.write_text(json.dumps(control))

    def write_config(self, config):
        paths.CONFIG.write_text(json.dumps(config))

    def config_bytes(self):
        return paths.CONFIG.read_bytes()

    def roster(self):
        return paths.config().get("opencode", [])

    def roster_ids(self):
        return [entry.get("sessionId") for entry in self.roster()]

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def native_creates(self):
        return [call for call in self.calls()
                if "POST" in call["argv"] and "/api/session" in call["argv"]]

    def native_sessions(self):
        with sqlite3.connect(self.native) as db:
            return [row[0] for row in db.execute("SELECT id FROM session_v2").fetchall()]

    def events(self):
        return [{"kind": kind, **json.loads(body)} for kind, body in
                self.db.execute("SELECT kind,body FROM events ORDER BY id").fetchall()]

    def provisioning(self):
        return self.db.execute("SELECT request_key,status,detail,payload_digest "
                               "FROM opencode_provisioning").fetchall()

    def session_receipts(self):
        return self.db.execute("SELECT session_id,request_key,status,detail "
                               "FROM opencode_sessions ORDER BY created").fetchall()

    def add(self, **kwargs):
        arguments = {"count": 1, "model": MODEL, "cwd": str(self.worktree),
                     "root": str(self.root)}
        arguments.update(kwargs)
        return opencode_lifecycle.add_sessions(self.db, **arguments)

    def ledger_task(self, task_id, sid, state):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (task_id, sid, str(self.worktree), json.dumps({"message": "m"}),
                         state, 1.0))
        self.db.commit()

    def queue_file(self, tasks):
        paths.QUEUE.parent.mkdir(parents=True, exist_ok=True)
        paths.QUEUE.write_text(json.dumps({"author": "root", "tasks": tasks}))

    def worktree_state(self):
        head = subprocess.run(["git", "-C", str(self.worktree), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
        branch = subprocess.run(["git", "-C", str(self.worktree), "symbolic-ref", "--short",
                                 "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        status = subprocess.run(["git", "-C", str(self.worktree), "status", "--porcelain"],
                                check=True, capture_output=True, text=True).stdout
        return {"head": head, "branch": branch, "status": status,
                "tracked": (self.worktree / "tracked.txt").read_text(),
                "untracked": (self.worktree / "untracked.txt").read_text()}

    # -- fixtures for a pre-existing request intent -------------------------
    def request_parts(self, count=1, target_total=None, enable_dispatch=False,
                      max_concurrent=None):
        """The key and the payload digest the module itself derives.

        The module's own functions are used, so the fixture cannot drift from
        the rule it exercises.
        """
        request = {"root": str(self.root), "account": str(self.native), "model": MODEL,
                   "cwd": str(self.worktree)}
        digest = opencode_lifecycle._payload_digest(request, count, target_total,
                                                     enable_dispatch, max_concurrent)
        key = opencode_lifecycle.derive_request_key(request, count, target_total,
                                                     enable_dispatch, max_concurrent)
        return key, digest

    def seed_intent(self, key, status, digest, creator_pid=0, detail="", count=1):
        """Write the durable intent of a request, as an earlier attempt left it."""
        self.db.execute(
            "INSERT INTO opencode_provisioning (request_key,model,account,cwd,root,"
            "requested_count,dispatch_enabled,status,created,updated,detail,payload_digest,"
            "creator_pid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, MODEL, str(self.native), str(self.worktree), str(self.root), count, 0,
             status, 1.0, 1.0, detail, digest, creator_pid))
        self.db.commit()

    def seed_receipt(self, session_id, key, status, detail=""):
        """Write a session receipt of a request, as an earlier attempt left it."""
        self.db.execute(
            "INSERT INTO opencode_sessions (session_id,request_key,native_id,model,account,cwd,"
            "root,status,created,updated,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, key, session_id, MODEL, str(self.native), str(self.worktree),
             str(self.root), status, 1.0, 1.0, detail))
        self.db.commit()

    def seed_native_session(self, session_id):
        """Write a native identity row, so a seeded receipt can be verified."""
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       (session_id, str(self.worktree), "project-independent",
                        json.dumps({"id": "minimax-m3.1-flash-free",
                                    "providerID": "opencode-go"})))


class NativeEffectTests(QualificationSandbox):
    """The native effect is exactly what the runtime asked for."""

    def test_the_native_call_carries_only_the_model_and_the_directory(self):
        self.add()
        creates = self.native_creates()
        self.assertEqual(len(creates), 1)
        payload = creates[0]["payload"]
        self.assertEqual(sorted(payload), ["location", "model"])
        self.assertEqual(sorted(payload["location"]), ["directory"])
        self.assertEqual(payload["location"]["directory"], str(self.worktree))
        model = payload["model"]
        if isinstance(model, dict):
            self.assertEqual(model, {"id": "minimax-m3.1-flash-free",
                                     "providerID": "opencode-go"})
        else:
            self.assertEqual(model, MODEL)
        self.assertNotIn("prompt", json.dumps(payload).lower())

    def test_the_created_session_is_read_back_from_the_synthetic_identity_row(self):
        result = self.add()
        self.assertEqual(result["status"], "registered")
        identifier = result["sessions"][0]["sessionId"]
        with sqlite3.connect(self.native) as db:
            row = db.execute("SELECT directory,model FROM session_v2 WHERE id=?",
                             (identifier,)).fetchone()
        self.assertIsNotNone(row, "the native identity row must exist")
        self.assertEqual(row[0], str(self.worktree))
        self.assertEqual(json.loads(row[1]), {"id": "minimax-m3.1-flash-free",
                                              "providerID": "opencode-go"})

    def test_the_native_request_body_conforms_to_the_schema_the_runtime_proved(self):
        """The body that is sent must satisfy the schema the runtime proved.

        The runtime proves the installed CLI's own schema before it creates a
        session. The request it then sends has to be an instance of that
        schema: a property the schema declares as an object reference cannot be
        sent as a plain string.
        """
        self.add()
        creates = self.native_creates()
        self.assertEqual(len(creates), 1)
        self.assertTrue(self.served.exists(), "the native server must have served its schema")
        schema = json.loads(self.served.read_text())
        declared = (schema["paths"]["/api/session"]["post"]["requestBody"]["content"]
                    ["application/json"]["schema"])
        resolved = schema["components"]["schemas"]
        payload = creates[0]["payload"]
        self.assertTrue(set(payload) <= set(declared["properties"]),
                        "the body must carry only declared properties")
        for name, value in payload.items():
            reference = declared["properties"][name].get("$ref")
            self.assertIsNotNone(reference, f"{name} must be declared as a schema reference")
            target = resolved[reference.rsplit("/", 1)[-1]]
            self.assertEqual(target.get("type"), "object",
                             f"{name} must be sent as the declared object")
            self.assertIsInstance(value, dict,
                                  f"{name} must be sent as the declared object, not "
                                  f"{type(value).__name__}")
            self.assertTrue(set(target.get("required", [])) <= set(value),
                            f"{name} must carry the required properties "
                            f"{target.get('required')}")

    def test_a_duplicate_native_identity_is_refused_and_the_roster_stays_exact(self):
        self.write_control({"mode": "duplicate", "idPrefix": "ses_SAME00000000000001"})
        result = self.add(count=2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["registered"], 0)
        self.assertFalse(result["rosterChanged"])
        self.assertEqual(self.roster_ids(), [self.existing])
        self.assertEqual(len(self.roster_ids()), len(set(self.roster_ids())))

    def test_the_registration_proof_counts_distinct_sessions_exactly(self):
        result = self.add(count=2)
        proof = result["proof"]
        self.assertEqual(proof["expected"], 2)
        self.assertEqual(proof["receipts"], 2)
        self.assertTrue(proof["distinct"])
        self.assertTrue(proof["inRoster"])
        self.assertEqual(proof["models"], [MODEL])
        self.assertEqual(len(self.roster()), 3)
        self.assertEqual(len(self.roster_ids()), len(set(self.roster_ids())))


class DryRunTests(QualificationSandbox):
    """A dry run reports the plan and changes nothing at all."""

    def test_a_dry_run_makes_no_native_call_and_writes_no_receipt(self):
        before = self.config_bytes()
        plan = self.add(dry_run=True)
        self.assertTrue(plan["dryRun"])
        self.assertEqual(plan["toCreate"], 1)
        self.assertEqual(self.native_creates(), [], "a dry run must not reach the native server")
        self.assertEqual(self.calls(), [], "a dry run must not call the native CLI at all")
        self.assertEqual(self.provisioning(), [])
        self.assertEqual(self.session_receipts(), [])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.config_bytes(), before)
        self.assertEqual(self.roster_ids(), [self.existing])

    def test_a_dry_run_of_a_target_total_writes_nothing(self):
        before = self.config_bytes()
        plan = self.add(count=None, target_total=3, dry_run=True)
        self.assertTrue(plan["dryRun"])
        self.assertEqual(plan["toCreate"], 2)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.config_bytes(), before)
        self.assertEqual(self.roster_ids(), [self.existing])


class RequestKeyBindingTests(QualificationSandbox):
    """A request key is bound to the whole request."""

    def test_the_same_key_with_a_different_payload_is_refused_before_any_native_call(self):
        self.add(count=1, request_key="w8-binding-001")
        self.assertEqual(len(self.native_creates()), 1)
        with self.assertRaises(ValueError) as error:
            self.add(count=2, request_key="w8-binding-001")
        self.assertIn("different request", str(error.exception))
        self.assertEqual(len(self.native_creates()), 1, "the conflict must not create a session")
        self.assertEqual(len(self.roster()), 2)

    def test_the_same_key_with_the_same_payload_is_idempotent(self):
        first = self.add(count=1, request_key="w8-binding-002")
        second = self.add(count=1, request_key="w8-binding-002")
        self.assertEqual(first["created"], 1)
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["created"], 0)
        self.assertEqual(len(self.native_creates()), 1)
        self.assertEqual(len(self.roster()), 2)


class CrashAndUncertaintyTests(QualificationSandbox):
    """A crash between the native effect and its receipt stays recoverable."""

    def test_a_crash_between_the_native_effect_and_its_receipt_creates_no_second_session(self):
        real_create = opencode_lifecycle.create_native_session

        def crash(*args, **kwargs):
            session = real_create(*args, **kwargs)
            raise KeyboardInterrupt("the fleet process died after the native effect")

        with patch.object(opencode_lifecycle, "create_native_session", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.add(count=1, request_key="w8-crash-001")
        created = self.native_creates()
        self.assertEqual(len(created), 1, "the native effect happened exactly once")
        native_ids = [row for row in self.native_sessions() if row != self.existing]
        self.assertEqual(len(native_ids), 1)
        self.assertEqual(self.roster_ids(), [self.existing], "the roster is untouched")
        self.assertEqual(self.session_receipts(), [], "no receipt was written")

        # The retry must not create a second session and must not register anything.
        with self.assertRaises(ValueError):
            self.add(count=1, request_key="w8-crash-001")
        self.assertEqual(len(self.native_creates()), 1, "the retry must not create again")
        self.assertEqual(self.roster_ids(), [self.existing])

    def test_a_crashed_creator_leaves_the_request_uncertain_and_not_retryable(self):
        real_create = opencode_lifecycle.create_native_session

        def crash(*args, **kwargs):
            session = real_create(*args, **kwargs)
            raise KeyboardInterrupt("the fleet process died after the native effect")

        with patch.object(opencode_lifecycle, "create_native_session", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.add(count=1, request_key="w8-crash-002")
        # The creator is gone, so the retry is an uncertain outcome rather than a
        # live attempt. Either way it must not create a session on its own.
        with patch.object(opencode_lifecycle, "_creator_alive", return_value=False):
            with self.assertRaises(ValueError) as error:
                self.add(count=1, request_key="w8-crash-002")
        message = str(error.exception)
        self.assertTrue("uncertain" in message or "in progress" in message, message)
        self.assertEqual(len(self.native_creates()), 1)
        self.assertEqual(self.roster_ids(), [self.existing])
        row = self.provisioning()
        self.assertEqual(len(row), 1)
        self.assertIn(row[0][1], {"creating", "uncertain"})

    def test_an_explicitly_named_session_is_registered_without_a_second_creation(self):
        real_create = opencode_lifecycle.create_native_session

        def crash(*args, **kwargs):
            session = real_create(*args, **kwargs)
            raise KeyboardInterrupt("the fleet process died after the native effect")

        with patch.object(opencode_lifecycle, "create_native_session", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.add(count=1, request_key="w8-crash-003")
        native_ids = [row for row in self.native_sessions() if row != self.existing]
        self.assertEqual(len(native_ids), 1)
        with patch.object(opencode_lifecycle, "_creator_alive", return_value=False):
            result = self.add(count=1, request_key="w8-crash-003",
                               reconciled_sessions=[native_ids[0]])
        self.assertEqual(result["registered"], 1)
        self.assertEqual(result["sessions"][0]["sessionId"], native_ids[0])
        self.assertEqual(len(self.native_creates()), 1, "the reconciliation must not create again")
        self.assertIn(native_ids[0], self.roster_ids())

    def test_a_native_effect_that_cannot_be_read_back_stays_uncertain(self):
        # The native server creates the session, then the identity row is removed
        # before the runtime reads it back: absence is not proof of no session.
        real_create = opencode_lifecycle.create_native_session

        def create_then_hide(*args, **kwargs):
            session = real_create(*args, **kwargs)
            with sqlite3.connect(self.native) as db:
                db.execute("DELETE FROM session_v2 WHERE id=?", (session["id"],))
            return session

        with patch.object(opencode_lifecycle, "create_native_session",
                          side_effect=create_then_hide):
            result = self.add(count=1, request_key="w8-uncertain-001")
        self.assertEqual(result["status"], "uncertain")
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["retryable"])
        self.assertFalse(result["rosterChanged"])
        self.assertEqual(self.roster_ids(), [self.existing])
        self.assertTrue(any(event.get("kind") == "needs_attention" for event in self.events()))


class QueueAndLedgerIntegrityTests(QualificationSandbox):
    """The queue and the ledger are read fail-closed."""

    def test_a_malformed_queue_file_does_not_let_a_removal_pass_unseen(self):
        paths.QUEUE.parent.mkdir(parents=True, exist_ok=True)
        paths.QUEUE.write_text("{ this is not json")
        with self.assertRaises(ValueError) as error:
            opencode_lifecycle.remove_session(self.db, self.existing,
                                              allow_unverified_native=True)
        self.assertIn("queue", str(error.exception).lower())
        self.assertIn(self.existing, self.roster_ids(),
                      "a worker must not be removed while its queue cannot be read")

    def test_an_unreadable_task_ledger_refuses_the_removal_and_keeps_the_roster(self):
        self.db.execute("DROP TABLE tasks")
        self.db.commit()
        with self.assertRaises(sqlite3.Error):
            opencode_lifecycle.remove_session(self.db, self.existing,
                                              allow_unverified_native=True)
        self.assertIn(self.existing, self.roster_ids(),
                      "a worker must not be removed while its ledger cannot be read")

    def test_a_terminal_task_in_the_queue_history_does_not_block_the_removal(self):
        # The queue file keeps the tasks it ever held. A task that was imported
        # and has finished is history, not work that is waiting to be imported.
        self.ledger_task("task-finished", self.existing, "finished")
        self.queue_file([{"id": "task-finished", "sessionId": self.existing,
                          "message": "an old task", "rootAuthorized": True}])
        opencode_lifecycle.remove_session(self.db, self.existing,
                                          allow_unverified_native=True)
        self.assertNotIn(self.existing, self.roster_ids())

    def test_a_queue_entry_that_names_the_worker_is_never_lost_by_a_drain(self):
        self.queue_file([{"id": "task-pending", "sessionId": self.existing,
                          "message": "queued work", "rootAuthorized": True}])
        before = paths.QUEUE.read_bytes()
        result = opencode_lifecycle.drain_session(self.db, self.existing)
        self.assertEqual([item["taskId"] for item in result["awaitingImport"]],
                         ["task-pending"])
        self.assertFalse(result["removed"])
        self.assertEqual(paths.QUEUE.read_bytes(), before,
                         "the drain must not touch the queue file")
        self.assertIn(self.existing, self.roster_ids())
        self.assertTrue(self.roster()[0].get("draining"))


class RemovalBarrierTests(QualificationSandbox):
    """The removal barriers protect the work and the worktree."""

    def test_a_running_worker_is_never_removed(self):
        with patch.object(opencode_lifecycle, "_native_state",
                          return_value={"state": "running"}):
            with self.assertRaises(ValueError) as error:
                opencode_lifecycle.remove_session(self.db, self.existing,
                                                  allow_unverified_native=True)
        self.assertIn("active", str(error.exception))
        self.assertIn(self.existing, self.roster_ids())

    def test_a_removal_keeps_the_native_session_and_every_hold(self):
        self.db.execute("INSERT INTO task_holds VALUES (?,?,?,?,?)",
                        ("task-hold-1", "an operator reason", "held", 1.0, 1.0))
        self.db.commit()
        opencode_lifecycle.remove_session(self.db, self.existing,
                                          allow_unverified_native=True)
        self.assertNotIn(self.existing, self.roster_ids())
        self.assertIn(self.existing, self.native_sessions(),
                      "the native session must survive the removal")
        holds = self.db.execute("SELECT status FROM task_holds WHERE task_id=?",
                                ("task-hold-1",)).fetchall()
        self.assertEqual(holds, [("held",)], "no hold may be released by a removal")

    def test_a_removal_leaves_a_dirty_unpublished_worktree_byte_identical(self):
        before = self.worktree_state()
        self.assertNotEqual(before["status"], "", "the fixture must start dirty")
        opencode_lifecycle.remove_session(self.db, self.existing,
                                          allow_unverified_native=True)
        after = self.worktree_state()
        self.assertEqual(before, after, "the worktree must not be touched by a removal")

    def test_an_active_worker_with_unpublished_work_is_never_removed(self):
        before = self.worktree_state()
        with patch.object(opencode_lifecycle, "_native_state",
                          return_value={"state": "running"}):
            with self.assertRaises(ValueError):
                opencode_lifecycle.remove_session(self.db, self.existing,
                                                  allow_unverified_native=True)
        self.assertEqual(before, self.worktree_state())
        self.assertIn(self.existing, self.roster_ids())


class BarrierRaceTests(QualificationSandbox):
    """Concurrent writers are separated by a barrier, not by luck."""

    def _run_concurrently(self, first, second):
        barrier = threading.Barrier(2, timeout=20)
        results = {}

        def wrap(name, function):
            def runner():
                try:
                    barrier.wait()
                    results[name] = function()
                except Exception as error:  # noqa: BLE001 - the failure is the result
                    results[name] = {"error": str(error)}
            return runner

        threads = [threading.Thread(target=wrap("a", first)),
                   threading.Thread(target=wrap("b", second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        return results

    def test_two_barrier_aligned_requests_with_one_key_make_exactly_one_native_call(self):
        def request():
            db = store.database(paths.DB)
            try:
                return opencode_lifecycle.add_sessions(
                    db, count=1, model=MODEL, cwd=str(self.worktree), root=str(self.root),
                    request_key="w8-race-001")
            finally:
                db.close()

        results = self._run_concurrently(request, request)
        self.assertEqual(len(self.native_creates()), 1,
                         "exactly one native session may be created for one key")
        self.assertEqual(len(self.roster()), 2, "the roster must hold one new worker")
        self.assertEqual(len(self.roster_ids()), len(set(self.roster_ids())))
        created = [value for value in results.values()
                   if isinstance(value, dict) and value.get("created") == 1]
        self.assertEqual(len(created), 1, "exactly one request may report the creation")
        for value in results.values():
            if isinstance(value, dict) and "error" in value:
                self.assertTrue("in progress" in value["error"] or "uncertain" in value["error"],
                                value["error"])

    def test_two_barrier_aligned_requests_with_one_key_and_different_payloads_conflict(self):
        def first():
            db = store.database(paths.DB)
            try:
                return opencode_lifecycle.add_sessions(
                    db, count=1, model=MODEL, cwd=str(self.worktree), root=str(self.root),
                    request_key="w8-race-002")
            finally:
                db.close()

        def second():
            db = store.database(paths.DB)
            try:
                return opencode_lifecycle.add_sessions(
                    db, count=2, model=MODEL, cwd=str(self.worktree), root=str(self.root),
                    request_key="w8-race-002")
            finally:
                db.close()

        results = self._run_concurrently(first, second)
        errors = [value["error"] for value in results.values()
                  if isinstance(value, dict) and "error" in value]
        self.assertTrue(errors, "one of the two conflicting requests must be refused")
        self.assertTrue(any("different request" in error or "in progress" in error
                            for error in errors), errors)
        self.assertEqual(len(self.roster_ids()), len(set(self.roster_ids())),
                         "no worker may be registered twice")
        self.assertLessEqual(len(self.roster()), 3)

    def test_a_drain_and_a_removal_of_one_worker_cannot_both_take_effect(self):
        def drain():
            db = store.database(paths.DB)
            try:
                return opencode_lifecycle.drain_session(db, self.existing)
            finally:
                db.close()

        def remove():
            db = store.database(paths.DB)
            try:
                return opencode_lifecycle.remove_session(db, self.existing,
                                                        allow_unverified_native=True)
            finally:
                db.close()

        self._run_concurrently(drain, remove)
        present = self.existing in self.roster_ids()
        draining = bool(self.roster()[0].get("draining")) if present else False
        self.assertFalse(present and draining,
                         "a worker cannot be removed and draining at the same time")
        self.assertIn(self.existing, self.native_sessions())

    def test_an_import_with_a_stale_roster_snapshot_never_binds_a_task_to_a_removed_worker(self):
        # The removal happens first, with no queue entry for the worker.
        opencode_lifecycle.remove_session(self.db, self.existing,
                                          allow_unverified_native=True)
        self.assertNotIn(self.existing, self.roster_ids())
        # The queue file receives a task for the removed worker afterwards.
        self.queue_file([{"id": "task-late", "sessionId": self.existing,
                          "message": "queued after the removal", "rootAuthorized": True}])
        # The manager's roster snapshot was taken before the removal.
        stale_slots = {self.existing: {"cwd": str(self.worktree),
                                        "sessionCwd": str(self.worktree),
                                        "databasePath": str(self.native),
                                        "model": MODEL, "provider": "opencode"}}
        try:
            manager.import_queue(self.db, stale_slots)
        except ValueError:
            pass
        rows = self.db.execute("SELECT id,sid FROM tasks").fetchall()
        self.assertNotIn(self.existing, [row[1] for row in rows],
                         "a task must never be bound to a worker that left the roster")
        self.assertEqual([task["id"] for task in json.loads(paths.QUEUE.read_text())["tasks"]],
                         ["task-late"], "the queue entry must not be lost")

    def test_a_model_selection_write_never_loses_a_roster_entry(self):
        selection_read = threading.Event()
        selection_done = threading.Event()
        real_plan = model_selection.plan

        def slow_plan(cfg, model, session_id=None, db=None):
            planned = real_plan(cfg, model, session_id, db)
            selection_read.set()
            selection_done.wait(20)
            return planned

        def selection():
            db = store.database(paths.DB)
            try:
                with patch.object(model_selection, "plan", side_effect=slow_plan):
                    return model_selection.apply(db, OTHER_APPROVED, self.existing, db=db)
            finally:
                db.close()

        def addition():
            db = store.database(paths.DB)
            try:
                return opencode_lifecycle.add_sessions(
                    db, count=1, model=MODEL, cwd=str(self.worktree), root=str(self.root),
                    request_key="w8-race-003")
            finally:
                db.close()

        worker = threading.Thread(target=selection)
        worker.start()
        self.assertTrue(selection_read.wait(20), "the selection must read the roster first")
        result = addition()
        selection_done.set()
        worker.join(30)
        self.assertEqual(len(self.native_creates()), 1)
        self.assertEqual(result.get("registered"), 1,
                         "the addition must have registered its worker before the write")
        self.assertIn(self.existing, self.roster_ids(),
                      "the model selection must not lose the roster entry it read")
        self.assertGreaterEqual(len(self.roster()), 2,
                                "the model selection must not lose a worker added meanwhile")


class ReceiptTruthfulnessTests(QualificationSandbox):
    """The receipts say what happened, and only what happened."""

    def test_the_status_report_counts_the_roster_and_the_receipts_truthfully(self):
        self.add(count=2, request_key="w8-status-001")
        report = opencode_lifecycle.status(self.db)
        self.assertEqual(report["logicalSessions"], len(self.roster()))
        self.assertEqual(report["registeredReceipts"], 2)
        self.assertEqual(len(report["receipts"]), 2)
        self.assertEqual(report["roster"][0]["sessionId"], self.existing)
        self.assertTrue(report["managerRunning"] in (True, False))
        self.assertEqual(report["maxAddCount"], opencode_lifecycle.MAX_ADD_COUNT)

    def test_a_recovery_receipt_refuses_a_turn_whose_cleanup_is_not_confirmed(self):
        sid = self.existing
        turn = "turn-independent-0001"
        session_dir = opencode.session_dir(sid)
        turn_dir = opencode.turn_dir(sid, turn)
        turn_dir.mkdir(parents=True, exist_ok=True)
        launch = {"turn": turn, "sessionId": sid, "taskId": "task-successor-1",
                  "cwd": str(self.worktree), "model": MODEL}
        (session_dir / "current.json").write_text(json.dumps(launch))
        receipts = {"sessionId": sid, "taskId": "task-successor-1", "turn": turn,
                    "cwd": str(self.worktree), "model": MODEL, "expectedModel": MODEL,
                    "state": "error", "finishedAt": 1000.0,
                    "managedProcessIdentity": {"pid": 999999, "pgid": 999999,
                                               "startTime": 1.0},
                    "cleanup": "unconfirmed"}
        (turn_dir / "state.json").write_text(json.dumps(dict(receipts)))
        (turn_dir / "result.json").write_text(json.dumps(dict(receipts)))
        slot = {"sessionId": sid, "cwd": str(self.worktree),
                "databasePath": str(self.native), "model": MODEL}
        ok, reason = recovery.verify_stopped_native_execution(slot, "task-successor-1")
        self.assertFalse(ok, "an unconfirmed cleanup must not be reported as stopped")
        self.assertIn("cleanup", reason)

    def test_a_recovery_receipt_refuses_a_turn_whose_process_is_still_live(self):
        sid = self.existing
        turn = "turn-independent-0002"
        session_dir = opencode.session_dir(sid)
        turn_dir = opencode.turn_dir(sid, turn)
        turn_dir.mkdir(parents=True, exist_ok=True)
        launch = {"turn": turn, "sessionId": sid, "taskId": "task-successor-2",
                  "cwd": str(self.worktree), "model": MODEL}
        (session_dir / "current.json").write_text(json.dumps(launch))
        identity = {"pid": os.getpid(), "pgid": os.getpgid(0), "startTime": 1.0}
        receipts = {"sessionId": sid, "taskId": "task-successor-2", "turn": turn,
                    "cwd": str(self.worktree), "model": MODEL, "expectedModel": MODEL,
                    "state": "error", "finishedAt": 1000.0,
                    "managedProcessIdentity": identity, "cleanup": "confirmed"}
        (turn_dir / "state.json").write_text(json.dumps(dict(receipts)))
        (turn_dir / "result.json").write_text(json.dumps(dict(receipts)))
        slot = {"sessionId": sid, "cwd": str(self.worktree),
                "databasePath": str(self.native), "model": MODEL}
        ok, reason = recovery.verify_stopped_native_execution(slot, "task-successor-2")
        self.assertFalse(ok, "a live process must never be reported as stopped")
        self.assertTrue(reason)


class ReviewFindingReproductionTests(QualificationSandbox):
    """Named reproductions for the review findings that are still applicable.

    Each test states one invariant in the product's own terms and reproduces it
    with a deterministic synthetic fixture. No test weakens an existing guard
    and no oracle is changed to turn a failure into a pass.
    """

    def _concurrently(self, first, second):
        barrier = threading.Barrier(2, timeout=20)
        results = {}

        def wrap(name, function):
            def runner():
                try:
                    barrier.wait()
                    results[name] = function()
                except Exception as error:  # noqa: BLE001 - the failure is the result
                    results[name] = {"error": str(error)}
            return runner

        threads = [threading.Thread(target=wrap("a", first)),
                   threading.Thread(target=wrap("b", second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        return results

    def test_a_same_key_request_never_creates_a_second_native_session_after_a_takeover(self):
        """The same key must never add twice, whatever the intent row says.

        The guard that keeps two same-key requests apart reads the creator
        identity recorded in the request's durable intent. A request that takes
        a key over from an attempt whose process is gone must leave that intent
        naming the live creator; if it does not, the guard is off for that key
        and both requests create native sessions.
        """
        key, digest = self.request_parts(count=2)
        # A native session an earlier attempt created and receipted.
        earlier = "ses_EARLIER00000000001"
        self.seed_native_session(earlier)
        self.seed_receipt(earlier, key, "created", "native session created")
        # The intent the attempt that died left behind: it names a creator that
        # is gone, so the guard that reads it cannot see a live attempt.
        self.seed_intent(key, "creating", digest, creator_pid=DEAD_PID,
                         detail="intent recorded before the native creation", count=2)

        def request():
            db = store.database(paths.DB)
            try:
                return opencode_lifecycle.add_sessions(
                    db, count=2, model=MODEL, cwd=str(self.worktree), root=str(self.root),
                    request_key=key)
            finally:
                db.close()

        self._concurrently(request, request)
        self.assertLessEqual(len(self.native_creates()), 1,
                             "one key must never create more than one native session")
        registered = [row for row in self.session_receipts() if row[2] == "registered"]
        identifiers = [row[0] for row in registered]
        self.assertEqual(len(identifiers), len(set(identifiers)),
                         "one key must never register the same session twice")

    def test_a_partial_create_crash_leaves_the_request_uncertain_instead_of_creating_again(self):
        """A crash in the middle of a creation must leave the request uncertain.

        The product's own contract is that an uncertain outcome stays uncertain
        and the next run adopts nothing on its own. A crash after one session
        was created and receipted, but while a second one was in flight, leaves
        that second session unknown: the retry must stop and ask for an explicit
        reconciliation instead of creating another one.
        """
        real_create = opencode_lifecycle.create_native_session
        calls = {"count": 0}

        def create_then_crash(*args, **kwargs):
            session = real_create(*args, **kwargs)
            calls["count"] += 1
            if calls["count"] == 2:
                raise KeyboardInterrupt("the fleet process died during the second creation")
            return session

        with patch.object(opencode_lifecycle, "create_native_session",
                          side_effect=create_then_crash):
            with self.assertRaises(KeyboardInterrupt):
                self.add(count=2, request_key="w8-partial-001")
        crashed = len(self.native_creates())
        self.assertEqual(crashed, 2, "the crashed attempt created two native sessions")
        self.assertEqual(len([row for row in self.session_receipts()]), 1,
                         "only the first session was receipted")

        # The crashed attempt's process is gone. An in-process crash cannot make
        # its own pid disappear, so the liveness check is modelled directly.
        outcome = {}
        with patch.object(opencode_lifecycle, "_creator_alive", return_value=False):
            try:
                outcome["result"] = self.add(count=2, request_key="w8-partial-001")
            except ValueError as error:
                outcome["error"] = str(error)
        self.assertEqual(len(self.native_creates()), crashed,
                         "a retry must not create a native session while the crashed "
                         "attempt's outcome is unknown")
        self.assertIn("error", outcome,
                      "the retry must refuse while the crashed attempt's outcome is unknown")

    def test_a_dry_run_does_not_mutate_a_receipt(self):
        """A dry run writes nothing: not a receipt, not a roster, not a native call.

        A request whose receipt is still unresolved is reconciled before the
        plan is reported. That reconciliation writes to the receipt table, so a
        dry run over such a request changes state it promised to leave alone.
        """
        key, digest = self.request_parts(count=1)
        unresolved = "ses_UNVERIFIED00000001"
        self.seed_native_session(unresolved)
        self.seed_receipt(unresolved, key, "unverified", "the identity could not be read")
        self.seed_intent(key, "creating", digest, creator_pid=DEAD_PID,
                         detail="intent recorded before the native creation", count=1)
        before = self.session_receipts()
        outcome = {}
        try:
            outcome["plan"] = self.add(count=1, request_key=key, dry_run=True)
        except Exception as error:  # noqa: BLE001 - a refusal is also an outcome
            outcome["error"] = str(error)
        after = self.session_receipts()
        self.assertEqual(before, after,
                         "a dry run must not change a receipt: "
                         f"{before} became {after}")
        self.assertEqual(self.native_creates(), [],
                         "a dry run must not reach the native server")
        self.assertEqual(self.events(), [], "a dry run must not emit an event")


if __name__ == "__main__":
    unittest.main()
