import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from fleet.heavy import run, try_acquire


class HeavyValidationTests(unittest.TestCase):
    def test_two_slots_and_reuse_after_release(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            first = try_acquire(directory)
            second = try_acquire(directory)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            try:
                self.assertIsNone(try_acquire(directory))
                first.close()
                replacement = try_acquire(directory)
                self.assertIsNotNone(replacement)
                replacement.close()
            finally:
                first.close()
                second.close()

    def test_stale_metadata_does_not_hold_a_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "slot-0.lock").write_text('{"pid":999999}')
            lease = try_acquire(directory)
            self.assertIsNotNone(lease)
            lease.close()

    def test_exit_status_and_cwd_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            result = run([sys.executable, "-c", "raise SystemExit(7)"], Path(folder))
            self.assertEqual(result, 7)
            lease = try_acquire(Path(folder))
            self.assertIsNotNone(lease)
            lease.close()

    def test_missing_command_releases_lease(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(run(["/nonexistent-fleet-test-command"], Path(folder)), 127)
            first = try_acquire(Path(folder))
            second = try_acquire(Path(folder))
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            first.close()
            second.close()

    def test_child_keeps_inherited_lock_after_parent_close(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            first = try_acquire(directory)
            second = try_acquire(directory)
            child = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys; print('ready', flush=True); sys.stdin.readline()"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                pass_fds=(first.fileno(),))
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                first.close()
                self.assertIsNone(try_acquire(directory))
                child.stdin.write("\n")
                child.stdin.flush()
                child.wait(timeout=5)
                replacement = try_acquire(directory)
                self.assertIsNotNone(replacement)
                replacement.close()
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
                child.stdin.close()
                child.stdout.close()
                first.close()
                second.close()

    def test_no_command(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(run([], Path(folder)), 2)
