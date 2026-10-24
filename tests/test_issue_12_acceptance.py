"""Issue #12 command lifecycle acceptance in an owned disposable root.

This starts the current source manager only against a temporary root with no
workers. It never uses the live Fleet root, session database or OpenCode
service.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "fleet"


class ManagerCommandLifecycleTests(unittest.TestCase):
    def test_start_status_pause_resume_and_stop_stay_inside_the_disposable_root(self):
        with tempfile.TemporaryDirectory(prefix="fleet-issue-12-") as temporary:
            root = Path(temporary).resolve()
            config = root / "config" / "fleet.local.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({
                "schemaVersion": 2,
                "worktreeRoot": str(root),
                "executables": {},
                "maxConcurrent": 3,
                "minQueuedPerWorker": 0,
                "pi": [],
                "claude": [],
                "opencode": [],
            }))
            data = root / "data"
            data.mkdir()
            pause = data / "dispatch.paused"
            pause.write_text("paused\n")
            environment = dict(os.environ, FLEET_ROOT=str(root), FLEET_CONFIG=str(config))

            def invoke(*arguments):
                result = subprocess.run(
                    [sys.executable, str(CLI), *arguments], cwd=root, env=environment,
                    capture_output=True, text=True, timeout=20, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)

            initial = invoke("status")
            self.assertTrue(initial["paused"])
            self.assertFalse(initial["processes"]["manager"]["running"])

            started = invoke("start")
            self.assertTrue(pause.exists(), "start must not implicitly resume dispatch")
            self.assertIn("pid", started["manager"])

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status_file = data / "status.json"
                if status_file.exists():
                    status = invoke("status")
                    if status["processes"]["manager"]["running"]:
                        break
                time.sleep(0.05)
            else:
                self.fail("the disposable manager did not publish a live status")
            self.assertTrue(status["paused"])
            self.assertEqual(status["sessions"], {})
            self.assertEqual(status["tasks"], {})

            resumed = invoke("resume")
            self.assertFalse(resumed["paused"])
            self.assertFalse(pause.exists())
            paused = invoke("pause")
            self.assertTrue(paused["paused"])
            self.assertTrue(pause.exists())

            stopped = invoke("stop", "--manager-only")
            self.assertIn("manager", stopped["stopped"])
            self.assertTrue(stopped["processes"]["manager"]["running"] is False)
            self.assertTrue(pause.exists(), "stop must preserve the dispatch pause")
            self.assertTrue((data / "supervisor.disabled").exists())
