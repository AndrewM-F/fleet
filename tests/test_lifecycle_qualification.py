"""Distinct lifecycle regressions recovered from the W8 independent qualifications.

Six published snapshots of an independent qualification suite for the Fleet
OpenCode session lifecycle (``docs/lifecycle-qualification-history.md`` lists
them) were compared with ``main`` by running each against ``main``'s source and
by measuring, test by test, which source branches only that test reaches. The
cumulative suite is 61 tests. Most of them exercise code that ``main``'s own
suite already reaches under other names, and those are not repeated here.

What follows is the part that is distinct:

* five tests that reach branches of the lifecycle that no other test in the
  repository reaches: a stale caller resuming after an uncertain attempt, a
  native effect that cannot be read back, an unreadable task ledger, two
  barrier-aligned requests that share a key but not a payload, and the status
  report's counts;
* two replay cases that the historical suite asserted under an older contract
  and that ``main`` has since changed on purpose. They are restated against the
  current contract, with the reason, and not dropped.

Every fixture is synthetic and disposable: the fleet root is a temporary
directory, the native identity inventory is a temporary SQLite file with the
shape the runtime reads (``session_v2``), the native session server is a fake
executable, and the git worktree is a temporary repository. Nothing here
contacts a provider, writes a real native database, starts a live manager,
touches a live fleet, or removes anything from the host.
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


def result_status(result):
    """The status a result carries, or the text of the error it raised."""
    if "error" in result:
        return "error"
    return result.get("status")


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


class CrashAndUncertaintyTests(QualificationSandbox):
    """A crash between the native effect and its receipt stays recoverable."""

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

    def test_an_unreadable_task_ledger_refuses_the_removal_and_keeps_the_roster(self):
        self.db.execute("DROP TABLE tasks")
        self.db.commit()
        with self.assertRaises(sqlite3.Error):
            opencode_lifecycle.remove_session(self.db, self.existing,
                                              allow_unverified_native=True)
        self.assertIn(self.existing, self.roster_ids(),
                      "a worker must not be removed while its ledger cannot be read")

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


class LifecycleReplayTests(QualificationSandbox):
    """Replays and removals the other suites do not state in these terms.

    Both cases were asserted by the historical qualification under the contract
    that existed when it ran. ``main`` has changed that contract on purpose, so
    each is restated against the current one rather than copied:

    * the removal of a worker replayed a second time used to be refused. Since
      the intent reconciliation (#9) it is idempotent and says so
      (``alreadyRemoved``), which ``tests/test_lifecycle_convergence.py`` pins;
      what must still hold is that the replay changes nothing;
    * the removal of a worker the roster holds without a receipt used to need
      no native identity. Since the stale-identity repair (#13) the native
      database is read on every removal path, so the worker is given the native
      identity row a real one has; what must still hold is that no receipt is
      invented and the removal is recorded.
    """

    def _added(self, key="w8-replay-001", count=1):
        result = self.add(count=count, request_key=key)
        self.assertEqual(result["status"], "registered")
        return result["sessions"][0]["sessionId"]

    def test_a_removal_replayed_twice_changes_nothing_and_says_so(self):
        """The second removal reports the worker already gone and moves nothing."""
        identifier = self._added()
        opencode_lifecycle.drain_session(self.db, identifier)
        first = opencode_lifecycle.remove_session(self.db, identifier,
                                                  allow_unverified_native=True)
        self.assertTrue(first["removed"])
        self.assertFalse(first.get("alreadyRemoved", False))
        self.assertEqual(self.receipt_status(identifier), "removed")
        events_after_first = len(self.events())
        second = opencode_lifecycle.remove_session(self.db, identifier,
                                                   allow_unverified_native=True)
        self.assertTrue(second["removed"])
        self.assertTrue(second["alreadyRemoved"],
                        "the replay must say the worker was already out of the roster")
        self.assertFalse(second["rosterChanged"])
        self.assertEqual(self.receipt_status(identifier), "removed",
                         "a replayed removal must not move the receipt")
        self.assertNotIn(identifier, self.roster_ids())
        self.assertEqual(len(self.events()), events_after_first,
                         "a replayed removal must not record a second removal")

    def test_the_removal_of_a_worker_without_a_receipt_leaves_a_trail(self):
        """A worker that has no receipt still has to leave a record somewhere."""
        stranger = "ses_NORECEIPT00000001"
        self.seed_native_session(stranger)
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
