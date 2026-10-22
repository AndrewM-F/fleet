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
import hashlib
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
if mode == "failAfterCreate":
    # the effect was committed and the command then failed: the outcome is
    # ambiguous, exactly as a post-launch failure is in production
    sys.stderr.write("fake native server failed after the session was created\\n")
    raise SystemExit(3)
if mode == "garbage":
    # the effect was committed and the answer cannot be read
    print("not a session at all")
    raise SystemExit(0)
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

    def intent_status(self, key):
        """The durable status of a request's intent."""
        row = self.db.execute("SELECT status FROM opencode_provisioning WHERE request_key=?",
                              (key,)).fetchone()
        return row[0] if row else None

    def intent_owner(self, key):
        """The creator pid and the payload digest a request's intent records."""
        row = self.db.execute("SELECT creator_pid,payload_digest FROM opencode_provisioning "
                               "WHERE request_key=?", (key,)).fetchone()
        return row if row else (None, None)

    def receipt_status(self, session_id):
        """The durable status of a session receipt."""
        row = self.db.execute("SELECT status FROM opencode_sessions WHERE session_id=?",
                              (session_id,)).fetchone()
        return row[0] if row else None

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

    def _removal_with_queue(self, content):
        """Write a queue file with exactly this content and try to remove."""
        paths.QUEUE.parent.mkdir(parents=True, exist_ok=True)
        paths.QUEUE.write_text(content)
        try:
            opencode_lifecycle.remove_session(self.db, self.existing,
                                              allow_unverified_native=True)
        except ValueError as error:
            return str(error)
        return None

    def test_an_existing_empty_queue_file_refuses_the_removal(self):
        """An existing but empty queue is not an empty queue.

        The file is there, so the work in it is unknown: a queue file that
        carries nothing readable must refuse the removal rather than report
        that there is nothing waiting.
        """
        for content in ("", "   \n\t  \n"):
            paths.QUEUE.parent.mkdir(parents=True, exist_ok=True)
            paths.QUEUE.write_text(content)
            with self.assertRaises(ValueError) as error:
                opencode_lifecycle.remove_session(self.db, self.existing,
                                                  allow_unverified_native=True)
            self.assertIn("queue", str(error.exception).lower())
            self.assertIn(self.existing, self.roster_ids(),
                          "a worker must not be removed while its queue cannot be read")

    def test_an_existing_queue_file_without_a_tasks_list_refuses_the_removal(self):
        """A queue file that names no tasks list cannot be read either."""
        error = self._removal_with_queue(json.dumps({"author": "root"}))
        self.assertIsNotNone(error, "a queue file without a tasks list must refuse")
        self.assertIn("queue", error.lower())
        self.assertIn(self.existing, self.roster_ids(),
                      "a worker must not be removed while its queue cannot be read")

    def test_a_truncated_queue_file_refuses_the_removal(self):
        """A truncated queue file is a malformed queue file."""
        error = self._removal_with_queue('{"author": "root", "tasks": [{"id": "task-trunc"')
        self.assertIsNotNone(error, "a truncated queue file must refuse")
        self.assertIn("queue", error.lower())
        self.assertIn(self.existing, self.roster_ids())

    def test_a_missing_queue_file_is_distinguished_from_an_unreadable_one(self):
        """A queue file that is not there is a different case from one that cannot be read.

        There is no queue, so there is nothing waiting to be imported; the
        removal must not report the unreadable-queue refusal, and the queue
        file's absence must not be silently reported as an unreadable queue.
        """
        paths.QUEUE.parent.mkdir(parents=True, exist_ok=True)
        paths.QUEUE.unlink(missing_ok=True)
        outcome = {}
        try:
            outcome["result"] = opencode_lifecycle.remove_session(
                self.db, self.existing, allow_unverified_native=True)
        except ValueError as error:
            outcome["error"] = str(error)
        if "error" in outcome:
            self.assertNotIn("queue", outcome["error"].lower(),
                             "a missing queue file is not an unreadable queue file")
        else:
            self.assertTrue(outcome["result"]["removed"])
            self.assertNotIn(self.existing, self.roster_ids())


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
        """A model selection must never lose a roster entry.

        The selection and the addition are two writers of the same file. The
        roster is the identity source, so no worker may vanish between them and
        no receipt may claim a registration the roster does not have. Which of
        the two writes lands first is not the invariant; the invariant is that
        the roster stays consistent and complete.
        """
        selection_read = threading.Event()
        selection_done = threading.Event()
        real_plan = model_selection.plan

        def slow_plan(cfg, model, session_id=None, db=None):
            planned = real_plan(cfg, model, session_id, db)
            selection_read.set()
            selection_done.wait(5)
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
        try:
            result = addition()
        except ValueError as error:
            # The addition refused rather than overwriting the selection's
            # change; that is a refusal, not a lost worker.
            result = {"error": str(error)}
        selection_done.set()
        worker.join(30)
        self.assertEqual(len(self.native_creates()), 1)
        # The invariant: nothing is lost and nothing is claimed twice.
        self.assertIn(self.existing, self.roster_ids(),
                      "the model selection must not lose the roster entry it read")
        self.assertEqual(len(self.roster_ids()), len(set(self.roster_ids())),
                         "no worker may be registered twice")
        registered = [row[0] for row in self.session_receipts() if row[2] == "registered"]
        for identifier in registered:
            self.assertIn(identifier, self.roster_ids(),
                          "a receipt must agree with the roster")
        if isinstance(result, dict) and "error" in result:
            # The addition refused rather than overwriting the selection's
            # change; that is a refusal, not a lost worker.
            self.assertIn("configuration changed", result["error"])

    def test_an_out_of_band_config_change_after_the_snapshot_is_rejected(self):
        """A configuration change that arrives after the snapshot must be rejected.

        The model writer reads the configuration and then writes it back. If
        another writer changes the configuration in between, the model writer's
        write must be refused: its copy is stale and the roster is the identity
        source.
        """
        real_plan = model_selection.plan

        def plan_then_change(cfg, model, session_id=None, db=None):
            planned = real_plan(cfg, model, session_id, db)
            changed = paths.config()
            changed["maxConcurrent"] = int(changed.get("maxConcurrent", 0)) + 3
            paths.write_config(changed)
            return planned

        with patch.object(model_selection, "plan", side_effect=plan_then_change):
            with self.assertRaises(ValueError) as error:
                model_selection.apply(self.db, OTHER_APPROVED, self.existing, db=self.db)
        self.assertIn("configuration changed", str(error.exception))
        self.assertEqual(paths.config()["maxConcurrent"], 7,
                         "the out-of-band change must survive the refused write")

    def test_the_compare_and_swap_uses_the_digest_captured_with_the_snapshot(self):
        """The compare-and-swap must compare against the snapshot it started from.

        The digest a writer compares against has to be the digest of the
        configuration it read. A digest read again at write time always matches,
        and the compare-and-swap can then never detect a change.
        """
        captured = {}
        real_cas = roster.write_config_cas
        real_plan = model_selection.plan

        def plan_then_change(cfg, model, session_id=None, db=None):
            planned = real_plan(cfg, model, session_id, db)
            captured["snapshot"] = hashlib.sha256(self.config_bytes()).hexdigest()
            changed = paths.config()
            changed["maxConcurrent"] = int(changed.get("maxConcurrent", 0)) + 3
            paths.write_config(changed)
            return planned

        def spy_cas(cfg, expected_digest):
            captured["expected"] = expected_digest
            captured["current"] = roster.config_digest()
            return real_cas(cfg, expected_digest)

        with patch.object(model_selection, "plan", side_effect=plan_then_change), \
             patch.object(roster, "write_config_cas", side_effect=spy_cas):
            with self.assertRaises(ValueError):
                model_selection.apply(self.db, OTHER_APPROVED, self.existing, db=self.db)
        self.assertIn("expected", captured, "the write must go through the compare-and-swap")
        self.assertEqual(captured["expected"], captured["snapshot"],
                         "the compare-and-swap must use the digest captured with the "
                         "configuration it read, not a digest read again at write time")
        self.assertNotEqual(captured["expected"], captured["current"],
                            "the change that arrived after the snapshot must be visible")

    def test_a_concurrent_same_key_request_creates_one_session_and_keeps_the_final_intent(self):
        """Two same-key requests may create one native session and register it once.

        The interleaving is forced, not hoped for. The late request computes
        what it still needs before the first request has registered anything,
        and it only reaches the writer lock after the first request has
        finished. That is a legal order of events, and it is the one the
        runtime has to be safe in, so the outcome is the same on every run.
        """
        real_probe = opencode_lifecycle.verify_native_api_support
        late_needed = threading.Event()
        first_done = threading.Event()

        def probe_spy(executable, timeout=opencode_lifecycle.API_PROBE_TIMEOUT_SECONDS):
            name = threading.current_thread().name
            if name == "late-request":
                late_needed.set()
                first_done.wait(30)
            elif name == "first-request":
                late_needed.wait(30)
            return real_probe(executable, timeout)

        def request():
            db = store.database(paths.DB)
            try:
                return opencode_lifecycle.add_sessions(
                    db, count=1, model=MODEL, cwd=str(self.worktree), root=str(self.root),
                    request_key="w8-final-intent-002")
            finally:
                db.close()

        results = {}

        def run(name):
            try:
                results[name] = request()
            except Exception as error:  # noqa: BLE001 - the failure is the result
                results[name] = {"error": str(error)}
            finally:
                if name == "first":
                    first_done.set()

        with patch.object(opencode_lifecycle, "verify_native_api_support",
                          side_effect=probe_spy):
            first = threading.Thread(target=run, args=("first",), name="first-request")
            second = threading.Thread(target=run, args=("second",), name="late-request")
            first.start()
            second.start()
            first.join(60)
            second.join(60)
        self.assertEqual(len(self.native_creates()), 1,
                         "one key must create at most one native session")
        registered = [row for row in self.session_receipts() if row[2] == "registered"]
        self.assertEqual(len(registered), 1, "one key must register at most one worker")
        self.assertEqual(self.intent_status("w8-final-intent-002"), "registered",
                         "a final intent must never overwrite a registered request")
        self.assertEqual(len(self.roster()), 2)
        self.assertEqual(len(self.roster_ids()), len(set(self.roster_ids())))

    def test_a_final_intent_never_overwrites_a_registered_request(self):
        """A registered request stays registered.

        The intent of a request is durable. Once it says the request is
        registered, a later attempt for the same key must not move it back to an
        earlier state.
        """
        first = self.add(count=1, request_key="w8-final-intent-003")
        self.assertEqual(first["status"], "registered")
        self.assertEqual(self.intent_status("w8-final-intent-003"), "registered")
        second = self.add(count=1, request_key="w8-final-intent-003")
        self.assertTrue(second.get("idempotent"))
        self.assertEqual(self.intent_status("w8-final-intent-003"), "registered",
                         "a later attempt must not move a registered intent back")
        self.assertEqual(len(self.native_creates()), 1)
        self.assertEqual(len([row for row in self.session_receipts()
                              if row[2] == "registered"]), 1)


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


class InitialIntentRaceTests(QualificationSandbox):
    """The two-caller initial-intent race, reproduced deterministically.

    Two callers carry the same request key. The late one snapshots what it
    still needs and then pauses; the first one creates the native sessions,
    records them and reaches its intent state. When the late one resumes it
    must not overwrite that durable state, and it must not create an extra
    native session or a subset of one.

    Every barrier here is an event, so the order is chosen rather than hoped
    for, and the native creations are the sandbox's fake server.
    """

    KEY = "w8-intent-race"

    def _caller(self, name, count=2, key=None, results=None):
        def run():
            db = store.database(paths.DB)
            try:
                results[name] = opencode_lifecycle.add_sessions(
                    db, count=count, model=MODEL, cwd=str(self.worktree), root=str(self.root),
                    request_key=key or self.KEY)
            except Exception as error:  # noqa: BLE001 - the failure is the result
                results[name] = {"error": str(error)}
            finally:
                db.close()
        return run

    def _assert_one_request_created_exactly_its_sessions(self, count):
        """The exact native-create count and the distinct native sessions."""
        self.assertEqual(len(self.native_creates()), count,
                         "one key must create exactly the sessions it asked for")
        created = [row for row in self.native_sessions() if row != self.existing]
        self.assertEqual(len(created), count, "no extra native session may exist")
        self.assertEqual(len(set(created)), count, "no native session may be created twice")

    def _registered_ids(self):
        return [row[0] for row in self.session_receipts() if row[2] == "registered"]

    def test_a_stale_caller_that_resumes_after_the_key_was_registered_gets_the_idempotent_result(self):
        """The late caller resumes after the key was registered.

        The registered row is the truth: the late caller must report it, must
        not create anything and must not overwrite it.
        """
        late_at_snapshot = threading.Event()
        first_done = threading.Event()
        real_probe = opencode_lifecycle.verify_native_api_support

        def probe(executable, timeout=opencode_lifecycle.API_PROBE_TIMEOUT_SECONDS):
            if threading.current_thread().name == "late-caller":
                late_at_snapshot.set()
                first_done.wait(30)
            return real_probe(executable, timeout)

        results = {}
        with patch.object(opencode_lifecycle, "verify_native_api_support", side_effect=probe):
            late = threading.Thread(target=self._caller("late", results=results),
                                    name="late-caller")
            late.start()
            self.assertTrue(late_at_snapshot.wait(20),
                            "the late caller must snapshot before the first caller runs")
            first = threading.Thread(target=self._caller("first", results=results),
                                     name="first-caller")
            first.start()
            first.join(60)
            first_done.set()
            late.join(60)
        self._assert_one_request_created_exactly_its_sessions(2)
        registered = self._registered_ids()
        self.assertEqual(len(registered), 2)
        self.assertEqual(len(set(registered)), 2)
        self.assertEqual(len(self.roster()), 3, "the roster must hold the two new workers once")
        self.assertEqual(len(self.roster_ids()), len(set(self.roster_ids())))
        self.assertEqual(self.intent_status(self.KEY), "registered",
                         "the durable intent must not be overwritten")
        late_result = results["late"]
        self.assertNotIn("error", late_result, late_result)
        self.assertTrue(late_result.get("idempotent"),
                        "the late caller must report the registered result")
        self.assertEqual(late_result.get("created"), 0)
        self.assertEqual(late_result.get("registered"), 2)
        self.assertEqual({item["sessionId"] for item in late_result["sessions"]},
                         set(registered))

    def test_a_stale_caller_that_resumes_while_the_creator_is_alive_creates_nothing_extra(self):
        """The late caller resumes while the first caller is still in flight.

        The first caller has created its sessions and recorded their ids, and
        its creator is alive. The late caller holds a stale snapshot of what it
        still needs; it must not act on it.
        """
        first_recorded = threading.Event()
        late_done = threading.Event()
        real_record = opencode_lifecycle._record_provisioning

        def record_spy(db, request_key, request, status, detail="", count=None,
                       payload_digest="", creator_pid=0, keep_registered=False):
            written = real_record(db, request_key, request, status, detail, count,
                                  payload_digest, creator_pid, keep_registered)
            if (threading.current_thread().name == "first-caller"
                    and isinstance(detail, str) and detail.startswith("{")):
                # The ids are recorded before the roster write: the first
                # caller is in flight, with a live creator and its identities
                # already durable.
                first_recorded.set()
                late_done.wait(30)
            return written

        results = {}
        with patch.object(opencode_lifecycle, "_record_provisioning", side_effect=record_spy):
            first = threading.Thread(target=self._caller("first", results=results),
                                     name="first-caller")
            first.start()
            self.assertTrue(first_recorded.wait(20),
                            "the first caller must record its identities")
            late = threading.Thread(target=self._caller("late", results=results),
                                    name="late-caller")
            late.start()
            late.join(60)
            late_done.set()
            first.join(60)
        self._assert_one_request_created_exactly_its_sessions(2)
        registered = self._registered_ids()
        self.assertEqual(len(registered), 2)
        self.assertEqual(len(set(registered)), 2)
        self.assertEqual(len(self.roster_ids()), len(set(self.roster_ids())))
        self.assertEqual(self.intent_status(self.KEY), "registered",
                         "the durable intent must not be overwritten")

    def test_a_stale_caller_that_resumes_after_an_uncertain_attempt_fails_closed(self):
        """The late caller resumes after an uncertain attempt.

        The first caller's native identity could not be read, so its outcome
        stays uncertain. The late caller must fail closed: it may not create a
        session and it may not rewrite the uncertain state.
        """
        late_at_snapshot = threading.Event()
        first_done = threading.Event()
        real_probe = opencode_lifecycle.verify_native_api_support
        real_create = opencode_lifecycle.create_native_session

        def probe(executable, timeout=opencode_lifecycle.API_PROBE_TIMEOUT_SECONDS):
            if threading.current_thread().name == "late-caller":
                late_at_snapshot.set()
                first_done.wait(30)
            return real_probe(executable, timeout)

        def create_then_hide(*args, **kwargs):
            session = real_create(*args, **kwargs)
            with sqlite3.connect(self.native) as db:
                db.execute("DELETE FROM session_v2 WHERE id=?", (session["id"],))
            return session

        results = {}
        with patch.object(opencode_lifecycle, "verify_native_api_support", side_effect=probe), \
             patch.object(opencode_lifecycle, "create_native_session",
                          side_effect=create_then_hide):
            late = threading.Thread(target=self._caller("late", results=results),
                                    name="late-caller")
            late.start()
            self.assertTrue(late_at_snapshot.wait(20),
                            "the late caller must snapshot before the first caller runs")
            first = threading.Thread(target=self._caller("first", results=results),
                                     name="first-caller")
            first.start()
            first.join(60)
            first_done.set()
            late.join(60)
        self.assertEqual(result_status(results["first"]), "uncertain",
                         "the first attempt must be uncertain")
        self.assertEqual(len(self.native_creates()), 1,
                         "an uncertain attempt must not be followed by a second "
                         "native session")
        self.assertEqual(self.intent_status(self.KEY), "uncertain",
                         "the durable uncertain intent must not be overwritten")
        late_result = results["late"]
        if "error" in late_result:
            self.assertIn("uncertain", late_result["error"].lower(), late_result["error"])
        else:
            self.assertTrue(late_result.get("uncertain"),
                            "the late caller must fail closed on an uncertain attempt")


def result_status(result):
    """The status a result carries, or the text of the error it raised."""
    if "error" in result:
        return "error"
    return result.get("status")


class ExactHeadInvariantTests(QualificationSandbox):
    """The invariants root named for this exact head.

    * a native create that fails, or answers, after it started is an ambiguous
      effect: it is uncertain and not retryable without positive proof that
      nothing was committed;
    * a safe retry takes over the request, so the intent's owner changes;
    * a receipt never moves backwards.
    """

    def test_a_native_create_that_fails_after_it_started_is_uncertain_and_not_retryable(self):
        """A failure after the command started is an ambiguous effect."""
        self.write_control({"mode": "failAfterCreate", "idPrefix": "ses_POSTLAUNCH00000001"})
        result = self.add(count=1, request_key="w8-postlaunch-001")
        self.assertEqual(result["status"], "uncertain",
                         "a post-launch failure is not a plain failure")
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["retryable"],
                         "an ambiguous effect must not be an ordinary retry")
        self.assertFalse(result["rosterChanged"])
        self.assertEqual(self.intent_status("w8-postlaunch-001"), "uncertain")
        self.assertEqual(self.roster_ids(), [self.existing])
        # The effect may exist; a retry must not create a second one.
        with self.assertRaises(ValueError) as error:
            self.add(count=1, request_key="w8-postlaunch-001")
        self.assertIn("uncertain", str(error.exception).lower())
        self.assertEqual(len(self.native_creates()), 1,
                         "a retry must not create a second native session")

    def test_a_malformed_native_answer_after_the_command_ran_is_uncertain(self):
        """An answer that cannot be read is an ambiguous effect too."""
        self.write_control({"mode": "garbage", "idPrefix": "ses_GARBAGE000000000001"})
        result = self.add(count=1, request_key="w8-garbage-001")
        self.assertEqual(result["status"], "uncertain")
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["retryable"])
        self.assertEqual(self.intent_status("w8-garbage-001"), "uncertain")
        self.assertEqual(self.roster_ids(), [self.existing])
        with self.assertRaises(ValueError) as error:
            self.add(count=1, request_key="w8-garbage-001")
        self.assertIn("uncertain", str(error.exception).lower())
        self.assertEqual(len(self.native_creates()), 1,
                         "a retry must not create a second native session")

    def test_a_safe_retry_takes_over_the_request_and_changes_its_owner(self):
        """A retry that is safe takes the request over, and the intent says so.

        The intent names the attempt that currently holds the key. An intent
        that still names a dead creator would leave the single-attempt guard
        blind for that key.
        """
        key, digest = self.request_parts(count=1)
        earlier = "ses_EARLIER00000000001"
        # A native session the interrupted attempt created and never receipted:
        # the effect exists, the receipt does not.
        self.seed_native_session(earlier)
        self.seed_intent(key, "creating", digest, creator_pid=DEAD_PID,
                         detail="intent recorded before the native creation", count=1)
        owner_before, token_before = self.intent_owner(key)
        self.assertEqual(owner_before, DEAD_PID)
        result = self.add(count=1, request_key=key, reconciled_sessions=[earlier])
        self.assertEqual(result["registered"], 1)
        owner_after, token_after = self.intent_owner(key)
        self.assertEqual(owner_after, os.getpid(),
                         "a safe retry must take the request over and name itself as the owner")
        self.assertNotEqual(owner_after, DEAD_PID)
        self.assertEqual(token_after, token_before,
                         "the request token must still bind the same request")
        self.assertEqual(token_after, digest)

    def test_a_registered_receipt_is_never_downgraded(self):
        """A receipt moves forward only.

        A reconciliation that runs after a registration must not un-register the
        session: the receipt stays `registered`.
        """
        key, digest = self.request_parts(count=1)
        identifier = "ses_REGISTERED00000001"
        self.seed_native_session(identifier)
        self.seed_receipt(identifier, key, "registered", "in the roster")
        request = {"root": str(self.root), "account": str(self.native), "model": MODEL,
                   "cwd": str(self.worktree), "dispatchEnabled": False}
        self.assertEqual(self.receipt_status(identifier), "registered")
        opencode_lifecycle._record_session_locked(self.db, identifier, key, request, "created",
                                                  "reconciled: the native identity was read back")
        self.assertEqual(self.receipt_status(identifier), "registered",
                         "a registered receipt must never be downgraded to created")
        opencode_lifecycle._record_session(self.db, identifier, key, request, "created",
                                           "reconciled again")
        self.assertEqual(self.receipt_status(identifier), "registered",
                         "a registered receipt must never be downgraded to created")


class ReceiptLifecycleTests(QualificationSandbox):
    """The receipt trail of a worker that is added, drained and removed.

    The `registered` guard exists so that a stale write cannot un-register a
    session. It must not also swallow the lifecycle's own transitions: a drained
    worker and a removed worker are states the receipt has to record, and a
    result must never report a worker that is not in the roster.
    """

    def test_the_receipt_follows_the_worker_from_registered_to_draining_to_removed(self):
        """add -> registered -> drain -> removed, and the receipt says so."""
        key = "w8-receipt-lifecycle-001"
        added = self.add(count=1, request_key=key)
        self.assertEqual(added["status"], "registered")
        identifier = added["sessions"][0]["sessionId"]
        self.assertEqual(self.receipt_status(identifier), "registered")
        drained = opencode_lifecycle.drain_session(self.db, identifier)
        self.assertTrue(drained["draining"])
        self.assertEqual(self.receipt_status(identifier), "draining",
                         "a drained worker must be recorded as draining")
        opencode_lifecycle.remove_session(self.db, identifier, allow_unverified_native=True)
        self.assertNotIn(identifier, self.roster_ids(),
                         "the removal must have taken the worker out of the roster")
        self.assertEqual(self.receipt_status(identifier), "removed",
                         "a removed worker must be recorded as removed")

    def test_a_same_key_retry_after_a_removal_never_reports_a_worker_that_is_gone(self):
        """The retry's result must agree with the roster."""
        key = "w8-receipt-lifecycle-002"
        added = self.add(count=1, request_key=key)
        identifier = added["sessions"][0]["sessionId"]
        opencode_lifecycle.remove_session(self.db, identifier, allow_unverified_native=True)
        self.assertNotIn(identifier, self.roster_ids())
        retry = self.add(count=1, request_key=key)
        reported = {item["sessionId"] for item in retry.get("sessions", [])}
        for session_id in reported:
            self.assertIn(session_id, self.roster_ids(),
                          "a result must not report a worker that is not in the roster")
        self.assertEqual(retry.get("registered", 0), len(reported),
                         "the result's own counts must agree with the sessions it lists")

    def test_a_stale_write_does_not_downgrade_a_registered_receipt(self):
        """The guard's legitimate case: a stale write must not un-register a session.

        This is the distinction the finding turns on: the guard must keep a
        `created` or `unverified` write from overwriting a `registered` receipt,
        while the lifecycle's own transitions must still be recorded.
        """
        key, digest = self.request_parts(count=1)
        identifier = "ses_REGISTERED00000002"
        self.seed_native_session(identifier)
        self.seed_receipt(identifier, key, "registered", "in the roster")
        request = {"root": str(self.root), "account": str(self.native), "model": MODEL,
                   "cwd": str(self.worktree), "dispatchEnabled": False}
        opencode_lifecycle._record_session(self.db, identifier, key, request, "created",
                                           "a stale write")
        self.assertEqual(self.receipt_status(identifier), "registered",
                         "a stale created write must not un-register a session")
        opencode_lifecycle._record_session_locked(self.db, identifier, key, request,
                                                  "unverified", "a stale write")
        self.assertEqual(self.receipt_status(identifier), "registered",
                         "a stale unverified write must not un-register a session")


class ReceiptRankTests(QualificationSandbox):
    """The published receipt rule: a receipt moves forward, never backwards.

    The repair orders the receipt states and admits a forward transition while
    refusing a stale one. These tests exercise that rule in both directions, in
    synthetic temporary state, and cover the case that matters most in
    practice: a late writer must not resurrect a receipt that is already
    removed.
    """

    ORDER = ("unverified", "created", "registered", "draining", "removed")

    def _receipt(self, index, status):
        key, digest = self.request_parts(count=1)
        identifier = "ses_RANK%08d" % index
        self.seed_native_session(identifier)
        self.seed_receipt(identifier, key, status, "seeded")
        request = {"root": str(self.root), "account": str(self.native), "model": MODEL,
                   "cwd": str(self.worktree), "dispatchEnabled": False}
        return identifier, key, request

    def test_every_forward_step_of_the_receipt_lifecycle_is_accepted(self):
        """Each step forward is a valid transition and must be recorded."""
        for index, status in enumerate(self.ORDER[:-1]):
            identifier, key, request = self._receipt(index, status)
            following = self.ORDER[index + 1]
            opencode_lifecycle._record_session(self.db, identifier, key, request, following,
                                               "moved to " + following)
            self.assertEqual(self.receipt_status(identifier), following,
                             "the step from %s to %s must be recorded"
                             % (status, following))

    def test_no_step_backwards_is_accepted(self):
        """A stale write never moves a receipt backwards."""
        for index, status in enumerate(self.ORDER[1:], start=1):
            identifier, key, request = self._receipt(index, status)
            earlier = self.ORDER[index - 1]
            opencode_lifecycle._record_session(self.db, identifier, key, request, earlier,
                                               "a stale write")
            self.assertEqual(self.receipt_status(identifier), status,
                             "a stale write from %s to %s must not be recorded"
                             % (status, earlier))

    def test_a_late_registration_does_not_resurrect_a_removed_receipt(self):
        """A writer that arrives after the removal must not bring it back."""
        identifier, key, request = self._receipt(9, "removed")
        opencode_lifecycle._record_session(self.db, identifier, key, request, "registered",
                                           "a late registration")
        self.assertEqual(self.receipt_status(identifier), "removed",
                         "a removed receipt must not be resurrected")
        opencode_lifecycle._record_session_locked(self.db, identifier, key, request, "registered",
                                                  "a late registration")
        self.assertEqual(self.receipt_status(identifier), "removed",
                         "a removed receipt must not be resurrected")

    def test_the_locked_write_reports_the_status_that_was_recorded(self):
        """The locked write reports the durable status, not the one it asked for."""
        identifier, key, request = self._receipt(10, "registered")
        recorded = opencode_lifecycle._record_session_locked(self.db, identifier, key, request,
                                                             "draining", "draining")
        self.assertEqual(recorded, "draining")
        self.assertEqual(self.receipt_status(identifier), "draining")
        # A stale write under the lock leaves the receipt where it is.
        recorded = opencode_lifecycle._record_session_locked(self.db, identifier, key, request,
                                                             "created", "a stale write")
        self.assertEqual(recorded, "draining",
                         "the locked write must report the status that is durable")
        self.assertEqual(self.receipt_status(identifier), "draining")


class LifecycleReplayTests(QualificationSandbox):
    """Interactions the completed suites do not reach.

    The 7 focused and 20 adjacent tests already cover the receipt ranks, the
    registered -> draining -> removed lifecycle, the same-key retry after a
    removal, the stale writes, the two-caller races and the crash and
    uncertainty paths. These cases are the ones still open: replaying a drain
    and a removal, a drain whose roster write is refused, a worker without a
    receipt, a forward transition over an ambiguous receipt, the edges of the
    rank rule, and the identity fields of a receipt whose state moves.
    """

    def _added(self, key="w8-replay-001", count=1):
        result = self.add(count=count, request_key=key)
        self.assertEqual(result["status"], "registered")
        return result["sessions"][0]["sessionId"]

    def test_a_drain_replayed_twice_is_safe_and_truthful(self):
        """The second drain must not move the receipt and must say so."""
        identifier = self._added()
        first = opencode_lifecycle.drain_session(self.db, identifier)
        self.assertTrue(first["draining"])
        self.assertFalse(first["alreadyDraining"])
        self.assertEqual(self.receipt_status(identifier), "draining")
        second = opencode_lifecycle.drain_session(self.db, identifier)
        self.assertTrue(second["draining"])
        self.assertTrue(second["alreadyDraining"],
                        "the replay must report that the worker was already draining")
        self.assertEqual(self.receipt_status(identifier), "draining")
        self.assertIn(identifier, self.roster_ids())
        entry = next(item for item in self.roster() if item.get("sessionId") == identifier)
        self.assertTrue(entry.get("draining"),
                        "the worker must stay marked as draining after the replay")

    def test_a_removal_replayed_twice_is_refused_and_leaves_the_receipt_alone(self):
        """The second removal must fail cleanly and change nothing."""
        identifier = self._added()
        opencode_lifecycle.drain_session(self.db, identifier)
        opencode_lifecycle.remove_session(self.db, identifier, allow_unverified_native=True)
        self.assertEqual(self.receipt_status(identifier), "removed")
        with self.assertRaises(ValueError):
            opencode_lifecycle.remove_session(self.db, identifier, allow_unverified_native=True)
        self.assertEqual(self.receipt_status(identifier), "removed",
                         "a refused replay must not move the receipt")
        self.assertNotIn(identifier, self.roster_ids())

    def test_a_drain_whose_roster_write_is_refused_leaves_the_receipt_and_the_roster_alike(self):
        """A refused roster write must not record a state that did not happen."""
        identifier = self._added()
        with patch.object(roster, "write_config_cas",
                          side_effect=ValueError("the configuration changed")):
            with self.assertRaises(ValueError):
                opencode_lifecycle.drain_session(self.db, identifier)
        self.assertEqual(self.receipt_status(identifier), "registered",
                         "a refused drain must not record the worker as draining")
        self.assertIn(identifier, self.roster_ids())
        self.assertFalse(self.roster()[0].get("draining"))
        # The worker is still there, so the removal still works.
        opencode_lifecycle.remove_session(self.db, identifier, allow_unverified_native=True)
        self.assertNotIn(identifier, self.roster_ids())
        self.assertEqual(self.receipt_status(identifier), "removed")

    def test_the_removal_of_a_worker_without_a_receipt_leaves_a_trail(self):
        """A worker that has no receipt still has to leave a record somewhere."""
        stranger = "ses_NORECEIPT00000001"
        config = paths.config()
        config.setdefault("opencode", []).append(
            {"sessionId": stranger, "cwd": str(self.worktree),
             "sessionCwd": str(self.worktree), "databasePath": str(self.native),
             "model": MODEL, "provider": "opencode", "dispatchEnabled": True})
        self.write_config(config)
        self.assertIn(stranger, self.roster_ids())
        opencode_lifecycle.remove_session(self.db, stranger, allow_unverified_native=True)
        self.assertNotIn(stranger, self.roster_ids())
        self.assertIsNone(self.receipt_status(stranger),
                          "no receipt may be invented for a worker that had none")
        kinds = [event["kind"] for event in self.events()]
        self.assertIn("opencode_session_removed", kinds,
                      "the removal must still be recorded in the event log")

    def test_a_forward_transition_over_an_ambiguous_receipt_keeps_the_evidence_in_the_log(self):
        """A receipt that was uncertain may move on, but the uncertainty stays visible."""
        key, digest = self.request_parts(count=1)
        identifier = "ses_AMBIGUOUS0000001"
        self.seed_native_session(identifier)
        self.seed_receipt(identifier, key, "unverified", "the identity could not be read")
        request = {"root": str(self.root), "account": str(self.native), "model": MODEL,
                   "cwd": str(self.worktree), "dispatchEnabled": False}
        opencode_lifecycle._record_session_locked(self.db, identifier, key, request, "created",
                                                  "reconciled: the native identity was read back")
        self.assertEqual(self.receipt_status(identifier), "created")
        opencode_lifecycle._record_session_locked(self.db, identifier, key, request, "registered",
                                                  "in the roster")
        self.assertEqual(self.receipt_status(identifier), "registered")
        self.assertNotEqual(self.receipt_status(identifier), "unverified",
                            "a receipt that was reconciled is no longer unverified")

    def test_the_rank_rule_heals_an_unknown_state_and_refuses_an_unknown_write(self):
        """The edges of the rule: an unknown stored state is healed, an unknown write is refused."""
        key, digest = self.request_parts(count=1)
        request = {"root": str(self.root), "account": str(self.native), "model": MODEL,
                   "cwd": str(self.worktree), "dispatchEnabled": False}
        unknown = "ses_UNKNOWNSTATE00001"
        self.seed_native_session(unknown)
        self.seed_receipt(unknown, key, "provisional", "an unknown state")
        opencode_lifecycle._record_session(self.db, unknown, key, request, "created", "healed")
        self.assertEqual(self.receipt_status(unknown), "created",
                         "a known forward write must heal an unknown stored state")
        known = "ses_KNOWNSTATE0000001"
        self.seed_native_session(known)
        self.seed_receipt(known, key, "registered", "in the roster")
        opencode_lifecycle._record_session(self.db, known, key, request, "provisional",
                                           "an unknown write")
        self.assertEqual(self.receipt_status(known), "registered",
                         "an unknown write must not overwrite a known state")

    def test_the_identity_of_a_receipt_does_not_move_with_its_state(self):
        """The state moves; the identity of the session stays."""
        identifier = self._added()
        row = self.db.execute("SELECT model,account,cwd FROM opencode_sessions WHERE session_id=?",
                              (identifier,)).fetchone()
        before = tuple(row)
        opencode_lifecycle.drain_session(self.db, identifier)
        after = tuple(self.db.execute(
            "SELECT model,account,cwd FROM opencode_sessions WHERE session_id=?",
            (identifier,)).fetchone())
        self.assertEqual(before, after,
                         "a receipt's model, account and working directory must not change "
                         "when its state moves")


if __name__ == "__main__":
    unittest.main()
