"""The pinned live manager: what it reloads, and what it cannot.

These tests run the **exact pinned manager code** that the live fleet runs, in
a temporary root with a synthetic configuration. They never touch the live
fleet, the live configuration or the live native database.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import paths  # noqa: E402


def pinned_installation():
    """The installation the live manager runs from, if it can be found."""
    override = os.environ.get("FLEET_PINNED_INSTALLATION")
    if override and (Path(override) / "src" / "fleet" / "manager.py").exists():
        return Path(override)
    try:
        listing = subprocess.run(["ps", "-ax", "-o", "command="], capture_output=True, text=True,
                                 timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in listing.splitlines():
        parts = line.split()
        if "run" not in parts:
            continue
        for part in parts:
            if not part.endswith("bin/fleet"):
                continue
            script = Path(part).resolve()
            root = script.parent.parent
            if (root / "src" / "fleet" / "manager.py").exists():
                return root
    return None


PINNED = pinned_installation()
FIRST = "ses_PINNED000000000001"
SECOND = "ses_PINNED000000000002"


class PinnedManagerReloadTests(unittest.TestCase):
    """The pinned manager reloads the roster and the admission limit per cycle."""

    @classmethod
    def setUpClass(cls):
        if PINNED is None:
            raise unittest.SkipTest("the pinned manager installation is not on this machine")
        cls.pinned = PINNED

    def _synthetic_root(self, tmp, sessions):
        root = Path(tmp)
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "data").mkdir(parents=True, exist_ok=True)
        worktree = root / "worktree"
        worktree.mkdir(exist_ok=True)
        database = root / "opencode.db"
        database.write_bytes(b"")
        executable = root / "fake-opencode"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        config = {"schemaVersion": 2, "worktreeRoot": str(root),
                  "executables": {"opencode": str(executable)},
                  "maxConcurrent": 1, "minQueuedPerWorker": 0,
                  "pi": [], "claude": [],
                  "opencode": [{"sessionId": sid, "cwd": str(worktree),
                                "sessionCwd": str(worktree), "databasePath": str(database),
                                "model": "opencode-go/minimax-m3.1-flash-free",
                                "provider": "opencode", "dispatchEnabled": True}
                               for sid in sessions]}
        (root / "config" / "fleet.local.json").write_text(json.dumps(config))
        return root, config

    def test_a_new_roster_entry_is_observed_without_restarting_the_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config = self._synthetic_root(tmp, [FIRST])
            environment = dict(os.environ, FLEET_ROOT=str(root),
                               FLEET_CONFIG=str(root / "config" / "fleet.local.json"))
            manager = subprocess.Popen([sys.executable, str(self.pinned / "bin" / "fleet"), "run"],
                                       env=environment, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                status = root / "data" / "status.json"
                deadline = time.time() + 60
                while time.time() < deadline and not status.exists():
                    time.sleep(0.2)
                self.assertTrue(status.exists(), "the pinned manager wrote no status")
                observed = json.loads(status.read_text())["sessions"]
                self.assertIn(FIRST, observed)
                # A new entry is added while the manager runs.
                config["opencode"].append({"sessionId": SECOND,
                                           "cwd": str(root / "worktree"),
                                           "sessionCwd": str(root / "worktree"),
                                           "databasePath": str(root / "opencode.db"),
                                           "model": "opencode-go/minimax-m3.1-flash-free",
                                           "provider": "opencode", "dispatchEnabled": True})
                (root / "config" / "fleet.local.json").write_text(json.dumps(config))
                deadline = time.time() + 60
                while time.time() < deadline:
                    current = json.loads(status.read_text())["sessions"]
                    if SECOND in current:
                        break
                    time.sleep(0.2)
                current = json.loads(status.read_text())["sessions"]
                self.assertIn(SECOND, current)
                self.assertEqual(manager.poll(), None, "the manager was replaced, not reloaded")
            finally:
                manager.terminate()
                manager.wait(timeout=20)

    def test_the_pinned_manager_reads_the_admission_limit_again_every_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config = self._synthetic_root(tmp, [FIRST])
            config_path = str(root / "config" / "fleet.local.json")
            script = (
                "import json, os, sys\n"
                f"sys.path.insert(0, {str(self.pinned / 'src')!r})\n"
                "from fleet import manager\n"
                f"CONFIG = {config_path!r}\n"
                "seen = []\n"
                "def fake_dispatch(db, slots, states, sender, paused=False, max_concurrent=0):\n"
                "    seen.append(max_concurrent)\n"
                "    if len(seen) == 1:\n"
                "        # The operator raises the limit while the manager runs.\n"
                "        cfg = json.load(open(CONFIG))\n"
                "        cfg['maxConcurrent'] = 7\n"
                "        open(CONFIG, 'w').write(json.dumps(cfg))\n"
                "    if len(seen) >= 2:\n"
                "        print(json.dumps(seen), flush=True)\n"
                "        os._exit(0)\n"
                "manager.dispatch = fake_dispatch\n"
                "manager.time.sleep = lambda *_: None\n"
                "manager.run()\n")
            environment = dict(os.environ, FLEET_ROOT=str(root),
                               FLEET_CONFIG=str(root / "config" / "fleet.local.json"))
            process = subprocess.Popen([sys.executable, "-c", script], env=environment,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                stdout, _ = process.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, _ = process.communicate()
                self.fail("the pinned manager did not run two cycles")
            self.assertIn("[1, 7]", stdout,
                          "the admission limit was not read again: " + stdout[-200:])

    def test_the_pinned_manager_does_not_honour_the_draining_flag(self):
        source = (self.pinned / "src" / "fleet" / "dispatch.py").read_text()
        self.assertNotIn("draining", source,
                         "the pinned dispatch honours the draining flag; the plan may be relaxed")
        manager_source = (self.pinned / "src" / "fleet" / "manager.py").read_text()
        self.assertIn("paths.opencode_slots()", manager_source,
                      "the pinned manager no longer reloads the roster per cycle")
        self.assertIn('paths.config().get("maxConcurrent"', manager_source,
                      "the pinned manager no longer reads the admission limit per cycle")

    def test_the_pinned_store_does_not_know_the_receipt_tables(self):
        source = (self.pinned / "src" / "fleet" / "store.py").read_text()
        self.assertNotIn("opencode_provisioning", source)
        self.assertNotIn("opencode_sessions", source)
        # The new tables are additive, so the pinned manager keeps working with
        # them present: it never refers to them and SQLite ignores them.
        new_source = (Path(__file__).resolve().parents[1] / "src" / "fleet" / "store.py").read_text()
        self.assertIn("CREATE TABLE IF NOT EXISTS opencode_provisioning", new_source)
        self.assertIn("CREATE TABLE IF NOT EXISTS opencode_sessions", new_source)

    def test_the_heavy_job_admission_is_separate_from_the_agent_count(self):
        source = (self.pinned / "src" / "fleet" / "heavy.py").read_text()
        self.assertIn("SLOT_COUNT = 2", source)
        self.assertNotIn("maxConcurrent", source,
                          "the heavy-job admission must not follow the agent count")


if __name__ == "__main__":
    unittest.main()
