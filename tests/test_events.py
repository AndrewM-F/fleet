"""Exact-task Pi completion attribution regressions; synthetic transcripts only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fleet import events, paths, store


class PiReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "session.jsonl"
        self.db = store.database(self.root / "test.sqlite")
        self.slot = {"sessionId": "s", "cwd": str(self.root), "provider": "pi",
                     "model": "fixture", "thinking": "high", "sessionFile": str(self.source)}
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        ("one", "s", str(self.root), json.dumps({"message": "task one"}),
                         "finished", 111))
        self.db.execute("INSERT INTO events(kind,body,created) VALUES (?,?,?)",
                        ("task_sent", json.dumps({"taskId": "one", "sessionId": "s"}), 100))
        self.db.commit()
        self.roster_patch = patch.object(paths, "slots", return_value=([self.slot], []))
        self.receipts_patch = patch.object(paths, "RECEIPTS", self.root / "receipts")
        self.roster_patch.start()
        self.receipts_patch.start()

    def tearDown(self):
        self.receipts_patch.stop()
        self.roster_patch.stop()
        self.db.close()
        self.tmp.cleanup()

    def row(self, role, text, at, stop="stop"):
        return {"timestamp": at, "message": {"role": role, "stopReason": stop,
                "content": [{"type": "thinking", "thinking": "private fixture"},
                            {"type": "text", "text": text}],
                "usage": {"inputTokens": 1}, "provider": "fixture"}}

    def write(self, rows):
        self.source.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def capture(self):
        return events.capture(self.db, 652, {"taskId": "one", "sessionId": "s",
                                            "state": "idle"}, 111)

    def test_next_task_kickoff_cannot_replace_previous_completion(self):
        self.write([self.row("user", "task one", 100.1),
                    self.row("assistant", "completed one", 110),
                    self.row("user", "task two", 111.2),
                    self.row("assistant", "starting two", 114.58, "toolUse")])
        result = self.capture()
        self.assertEqual(result["summary"], "completed one")
        self.assertEqual(result["messageTimestamp"], 110)
        self.assertNotIn("private fixture", json.dumps(result))

    def test_even_terminal_next_task_text_is_outside_the_boundary(self):
        self.write([self.row("user", "task one", 100.1),
                    self.row("assistant", "completed one", 110),
                    self.row("user", "task two", 111.2),
                    self.row("assistant", "completed two", 114)])
        self.assertEqual(self.capture()["summary"], "completed one")

    def test_unrelated_prompt_in_same_time_window_cannot_match(self):
        self.write([self.row("user", "task one extra", 100.1),
                    self.row("assistant", "unrelated result", 110)])
        result = self.capture()
        self.assertIn("captureError", result)
        self.assertNotIn("summary", result)

    def test_tool_use_text_is_not_a_completed_answer(self):
        self.write([self.row("user", "task one", 100.1),
                    self.row("assistant", "will inspect", 110, "toolUse")])
        self.assertIn("captureError", self.capture())

    def test_new_prompt_before_observation_makes_attribution_uncertain(self):
        self.write([self.row("user", "task one", 100.1),
                    self.row("assistant", "completed one", 105),
                    self.row("user", "manual task", 108),
                    self.row("assistant", "manual answer", 110)])
        self.assertIn("captureError", self.capture())

    def test_old_identical_prompt_is_not_reused(self):
        self.write([self.row("user", "task one", 80),
                    self.row("assistant", "old result", 90)])
        self.assertIn("captureError", self.capture())

    def test_missing_dispatch_evidence_fails_closed(self):
        self.db.execute("DELETE FROM events")
        self.db.commit()
        self.write([self.row("user", "task one", 100.1),
                    self.row("assistant", "completed one", 110)])
        self.assertIn("captureError", self.capture())

    def test_legacy_receipt_is_preserved_and_recomputed_once(self):
        self.write([self.row("user", "task one", 100.1),
                    self.row("assistant", "completed one", 110)])
        paths.RECEIPTS.mkdir()
        old = {"eventId": 652, "summary": "starting two"}
        (paths.RECEIPTS / "652.json").write_text(json.dumps(old))
        result = self.capture()
        self.assertEqual(result["summary"], "completed one")
        self.assertEqual(result["captureVersion"], events.CAPTURE_VERSION)
        self.assertEqual(json.loads(Path(result["legacyReceiptFile"]).read_text()), old)
        self.source.unlink()
        self.assertEqual(self.capture(), result)


if __name__ == "__main__":
    unittest.main()
