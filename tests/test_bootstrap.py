"""Fresh configuration and process-identity regression coverage."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fleet import claude, cli, control, paths, store


class BootstrapTests(unittest.TestCase):
    def test_first_queue_is_created_only_for_valid_root_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue.json"
            with patch.object(paths, "QUEUE", queue), patch.object(paths, "QUEUE_LOCK", root / "queue.lock"), \
                    patch.object(paths, "slots", return_value=([], [{"sessionId": "a"}])):
                db = store.database(root / "test.sqlite")
                try:
                    with self.assertRaises(ValueError):
                        control.enqueue(db, [{"id": "bad", "sessionId": "a", "message": "work"}])
                    self.assertFalse(queue.exists())
                    task = {"id": "ok", "sessionId": "a", "rootAuthorized": True, "message": "work"}
                    self.assertFalse(control.enqueue(db, [task])[0]["alreadyQueued"])
                    self.assertTrue(control.enqueue(db, [task])[0]["alreadyQueued"])
                    self.assertEqual(len(json.loads(queue.read_text())["tasks"]), 1)
                finally:
                    db.close()

    def test_claude_only_doctor_does_not_require_pi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            slot = {"sessionId": "a", "cwd": tmp, "resume": False}
            with patch.object(paths, "config", return_value={}), \
                    patch.object(paths, "slots", return_value=([], [slot])), \
                    patch.object(paths, "DATA", root / "data"), \
                    patch.object(paths, "DB", root / "data" / "test.sqlite"), \
                    patch.object(paths, "executable", return_value="claude") as executable, \
                    patch.object(claude, "native_transcript", return_value=[]), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    cli.cmd_doctor(None)
                self.assertEqual(result.exception.code, 0)
                executable.assert_called_once_with("claude", {})

    def test_reused_linux_pid_is_not_a_worker(self):
        with patch.object(claude.sys, "platform", "linux"), \
                patch.object(claude.os, "kill"), \
                patch.object(Path, "read_bytes", return_value=b"python\0unrelated.py\0"):
            self.assertFalse(claude.wrapper_alive(7, Path("/turn/request.json")))
        with patch.object(claude.sys, "platform", "linux"), \
                patch.object(claude.os, "kill"), \
                patch.object(Path, "read_bytes", return_value=b"python\0fleet\0claude-turn\0/turn/request.json\0"):
            self.assertTrue(claude.wrapper_alive(7, Path("/turn/request.json")))
