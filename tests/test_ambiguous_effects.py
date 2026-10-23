"""The three root-reviewed findings: an ambiguous native effect, the retry's
ownership, and the monotonic receipt transition.

Every fixture is synthetic: a temporary fleet root, a synthetic native session
inventory and a fake OpenCode CLI. No real native session is created, no manager
is installed or activated, and no account, model, count, quota, live
configuration or pinned installation is touched.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fleet import opencode_lifecycle, paths, roster, store  # noqa: E402
from test_opencode_lifecycle import FAKE_CLI  # noqa: E402

MODEL = "opencode-go/minimax-m3.1-flash-free"


class AmbiguousEffectTests(unittest.TestCase):
    """Finding 1: after the command has launched, a failure is an unknown effect."""

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
        self.control = root / "control.json"
        self.log = root / "fake-cli.log"
        self.fake = root / "fake-opencode"
        self.write_fake()
        self.config = {"schemaVersion": 2, "worktreeRoot": str(root / "wt"),
                       "executables": {"opencode": str(self.fake)}, "maxConcurrent": 4,
                       "minQueuedPerWorker": 0, "pi": [], "claude": [],
                       "opencode": [{"sessionId": "ses_EXISTING000000000001",
                                     "cwd": str(self.wt), "sessionCwd": str(self.wt),
                                     "databasePath": str(self.native), "model": MODEL,
                                     "provider": "opencode", "dispatchEnabled": True}]}
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

    def write_fake(self, control=None):
        if control is not None:
            self.control.write_text(json.dumps(control))
        self.fake.write_text(FAKE_CLI.format(control=str(self.control), database=str(self.native),
                                             log=str(self.log)))
        self.fake.chmod(0o755)

    def native_sessions(self):
        with sqlite3.connect(self.native) as db:
            return [row[0] for row in db.execute("SELECT id FROM session_v2").fetchall()]

    def test_a_commit_then_a_nonzero_exit_is_uncertain_and_not_retryable(self):
        self.write_fake({"mode": "commitThenFail", "sessionId": "ses_COMMITTED000000000001"})
        result = opencode_lifecycle.add_sessions(
            self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-ambiguous-001")
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["retryable"])
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["rosterChanged"])
        # The session was committed in the account, and the roster did not claim
        # it: the outcome stays unknown.
        self.assertIn("ses_COMMITTED000000000001-0", self.native_sessions())
        self.assertEqual(len(paths.config()["opencode"]), 1)
        row = opencode_lifecycle._provisioning_row(self.db, "w5-ambiguous-001")
        self.assertEqual(row[7], "uncertain")

    def test_a_commit_then_an_unusable_id_is_uncertain_and_not_retryable(self):
        self.write_fake({"mode": "commitThenGarbage", "sessionId": "ses_COMMITTED000000000002"})
        result = opencode_lifecycle.add_sessions(
            self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-ambiguous-002")
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["retryable"])
        self.assertIn("ses_COMMITTED000000000002-0", self.native_sessions())
        self.assertEqual(len(paths.config()["opencode"]), 1)

    def test_a_command_that_never_started_stays_retryable(self):
        # The command cannot be started at all: nothing ran, so the failure is
        # definite and the request may be retried.
        unstartable = self.root / "unstartable-opencode"
        unstartable.write_text("#!/nonexistent/interpreter\n")
        unstartable.chmod(0o755)
        with self.assertRaises(opencode_lifecycle.ProvisioningError) as error:
            opencode_lifecycle.create_native_session(
                str(unstartable), str(self.native), str(self.wt), MODEL, self.root / "scratch")
        self.assertNotIsInstance(error.exception, opencode_lifecycle.ProvisioningUncertain)
        self.assertEqual(self.native_sessions(), [])

    def test_a_request_whose_command_cannot_start_is_refused_as_retryable(self):
        unstartable = self.root / "unstartable-opencode"
        unstartable.write_text("#!/nonexistent/interpreter\n")
        unstartable.chmod(0o755)
        self.config["executables"]["opencode"] = str(unstartable)
        paths.CONFIG.write_text(json.dumps(self.config))
        with self.assertRaises(opencode_lifecycle.ProvisioningError) as error:
            opencode_lifecycle.add_sessions(
                self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                request_key="w5-ambiguous-003")
        self.assertNotIsInstance(error.exception, opencode_lifecycle.ProvisioningUncertain)
        self.assertEqual(self.native_sessions(), [])


class RetryOwnershipTests(unittest.TestCase):
    """Finding 2: a retry takes the ownership of the request atomically."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wt = self.root / "wt" / "repo"
        self.wt.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.wt)], check=True)
        self.native = self.root / "opencode.db"
        with sqlite3.connect(self.native) as db:
            db.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, project_id TEXT, "
                       "model TEXT)")
        self.control = self.root / "control.json"
        self.control.write_text(json.dumps({"mode": "ok", "sessionId": "ses_RETRY000000000000001"}))
        self.log = self.root / "fake-cli.log"
        self.fake = self.root / "fake-opencode"
        self.fake.write_text(FAKE_CLI.format(control=str(self.control), database=str(self.native),
                                             log=str(self.log)))
        self.fake.chmod(0o755)
        (self.root / "config").mkdir()
        (self.root / "data").mkdir()

    def _write_config(self, executable):
        config = {"schemaVersion": 2, "worktreeRoot": str(self.root / "wt"),
                  "executables": {"opencode": str(executable)}, "maxConcurrent": 4,
                  "minQueuedPerWorker": 0, "pi": [], "claude": [],
                  "opencode": [{"sessionId": "ses_EXISTING000000000001",
                                "cwd": str(self.wt), "sessionCwd": str(self.wt),
                                "databasePath": str(self.native), "model": MODEL,
                                "provider": "opencode", "dispatchEnabled": True}]}
        (self.root / "config" / "fleet.local.json").write_text(json.dumps(config))

    def _run(self, executable, key, prelaunch_failure=False):
        """Run one request in its own process, so each has its own pid."""
        self._write_config(executable)
        script = (
            "import json, sys\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})\n"
            "from fleet import opencode_lifecycle, paths, store\n"
            "if " + ("True" if prelaunch_failure else "False") + ":\n"
            "    def refuse(*args, **kwargs):\n"
            "        raise opencode_lifecycle.ProvisioningError("
            "'The OpenCode CLI could not be started: OSError')\n"
            "    opencode_lifecycle.create_native_session = refuse\n"
            "db = store.database(paths.DB)\n"
            "try:\n"
            "    result = opencode_lifecycle.add_sessions(\n"
            "        db, count=1, model='opencode-go/minimax-m3.1-flash-free',\n"
            f"        cwd={str(self.wt)!r}, root={str(self.root)!r}, request_key={key!r})\n"
            "    print(json.dumps({'status': result.get('status'),\n"
            "                      'created': result.get('created'),\n"
            "                      'pid': __import__('os').getpid()}))\n"
            "except Exception as error:\n"
            "    print(json.dumps({'error': str(error),\n"
            "                      'pid': __import__('os').getpid()}))\n")
        environment = dict(os.environ, FLEET_ROOT=str(self.root),
                           FLEET_CONFIG=str(self.root / "config" / "fleet.local.json"))
        result = subprocess.run([sys.executable, "-c", script], env=environment,
                                capture_output=True, text=True, timeout=180)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_a_prelaunch_failure_then_a_retry_moves_the_ownership(self):
        # The first request cannot start the CLI at all: a definite failure with
        # no native effect, and the request row records the first process.
        unstartable = self.root / "unstartable-opencode"
        unstartable.write_text("#!/nonexistent/interpreter\n")
        unstartable.chmod(0o755)  # it is executable, but it cannot be started
        first = self._run(self.fake, "w5-ownership-001", prelaunch_failure=True)
        # The attempt failed before the command launched: a definite, retryable
        # failure, and the request row records the first process.
        self.assertEqual(first.get("status"), "failed")
        self.assertEqual(first.get("created"), 0)
        self.assertNotEqual(first["pid"], os.getpid())
        db = store.database(self.root / "data" / "fleet.sqlite")
        try:
            row = opencode_lifecycle._provisioning_row(db, "w5-ownership-001")
            self.assertEqual(row[7], "failed")
            self.assertEqual(row[12], first["pid"])
        finally:
            db.close()
        # The retry runs in its own process and takes the ownership atomically.
        second = self._run(self.fake, "w5-ownership-001")
        self.assertEqual(second.get("status"), "registered")
        self.assertEqual(second.get("created"), 1)
        self.assertNotEqual(second["pid"], first["pid"])
        db = store.database(self.root / "data" / "fleet.sqlite")
        try:
            row = opencode_lifecycle._provisioning_row(db, "w5-ownership-001")
            self.assertEqual(row[7], "registered")
            self.assertEqual(row[12], second["pid"])
        finally:
            db.close()
        config = json.loads((self.root / "config" / "fleet.local.json").read_text())
        self.assertEqual(len(config["opencode"]), 2)


class MonotonicReceiptTests(unittest.TestCase):
    """Finding 3: a reconciliation never downgrades a registered receipt."""

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
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       ("ses_PROMOTED00000000001", str(self.wt), "p", MODEL))
        self.control = root / "control.json"
        self.control.write_text(json.dumps({"mode": "ok", "sessionId": "ses_PROMOTED00000000001"}))
        self.log = root / "fake-cli.log"
        self.fake = root / "fake-opencode"
        self.fake.write_text(FAKE_CLI.format(control=str(self.control), database=str(self.native),
                                             log=str(self.log)))
        self.fake.chmod(0o755)
        (root / "config").mkdir()
        (root / "data").mkdir()
        config = {"schemaVersion": 2, "worktreeRoot": str(root / "wt"),
                  "executables": {"opencode": str(self.fake)}, "maxConcurrent": 4,
                  "minQueuedPerWorker": 0, "pi": [], "claude": [],
                  "opencode": [{"sessionId": "ses_EXISTING000000000001", "cwd": str(self.wt),
                                "sessionCwd": str(self.wt), "databasePath": str(self.native),
                                "model": MODEL, "provider": "opencode",
                                "dispatchEnabled": True}]}
        (root / "config" / "fleet.local.json").write_text(json.dumps(config))
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

    def test_a_promotion_never_downgrades_a_registered_receipt(self):
        identifier = "ses_PROMOTED00000000001"
        request = {"model": MODEL, "account": str(self.native), "cwd": str(self.wt),
                   "root": str(self.root), "count": 1, "dispatchEnabled": False}
        # The receipt is already registered.
        opencode_lifecycle._record_session(self.db, identifier, "w5-monotonic-001", request,
                                          "registered", "in the roster")
        # A reconciliation tries to promote it to created.
        opencode_lifecycle._record_session_locked(self.db, identifier, "w5-monotonic-001", request,
                                                 "created", "reconciled")
        row = self.db.execute("SELECT status FROM opencode_sessions WHERE session_id=?",
                              (identifier,)).fetchone()
        self.assertEqual(row[0], "registered")

    def test_a_promotion_racing_a_registration_keeps_the_registration(self):
        identifier = "ses_PROMOTED00000000002"
        request = {"model": MODEL, "account": str(self.native), "cwd": str(self.wt),
                   "root": str(self.root), "count": 1, "dispatchEnabled": False}
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?,?,?,?)",
                       (identifier, str(self.wt), "p", MODEL))
        verified = threading.Event()
        release = threading.Event()
        real_verify = opencode_lifecycle._verify_created
        outcome = {}

        def verify(session_id, cwd, account, model=None):
            result = real_verify(session_id, cwd, account, model)
            if threading.current_thread() is promotion_thread:
                # The reconciliation has verified the identity and pauses before
                # it writes the receipt.
                verified.set()
                release.wait(30)
            return result

        def promote():
            connection = store.database(paths.DB)
            try:
                outcome["result"] = opencode_lifecycle.add_sessions(
                    connection, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
                    request_key="w5-monotonic-002")
            except Exception as error:  # noqa: BLE001 - the refusal is the result
                outcome["error"] = str(error)
            finally:
                connection.close()

        # The receipt is unverified: the request reconciles it, which verifies
        # the identity and then promotes the receipt.
        opencode_lifecycle._record_session(self.db, identifier, "w5-monotonic-002", request,
                                          "unverified", "the identity was not read back")
        promotion_thread = threading.Thread(target=promote)
        with patch.object(opencode_lifecycle, "_verify_created", side_effect=verify):
            promotion_thread.start()
            self.assertTrue(verified.wait(30), "the promotion never verified the identity")
            # The registration lands while the promotion is paused.
            opencode_lifecycle._record_session(self.db, identifier, "w5-monotonic-002", request,
                                              "registered", "in the roster")
            release.set()
            promotion_thread.join(30)
        row = self.db.execute("SELECT status FROM opencode_sessions WHERE session_id=?",
                              (identifier,)).fetchone()
        self.assertEqual(row[0], "registered")


if __name__ == "__main__":
    unittest.main()
