import json
import tempfile
import unittest
from pathlib import Path
from fleet.events import pi_completion

class EmptyErrorCompletionTests(unittest.TestCase):
    def test_empty_terminal_error_is_a_correlated_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.jsonl"
            rows = [
                {"timestamp": 100, "message": {"role": "user", "content": "exact task"}},
                {"timestamp": 101, "message": {"role": "assistant", "content": [],
                                            "stopReason": "error", "provider": "anthropic"}},
            ]
            source.write_text("\n".join(json.dumps(row) for row in rows))
            found = pi_completion(source, "exact task", 99, 102)
            self.assertIsNotNone(found)
            self.assertEqual(found[0]["stopReason"], "error")
            self.assertEqual(found[1], "")
            self.assertIsNone(pi_completion(source, "other task", 99, 102))
            self.assertIsNone(pi_completion(source, "exact task", 99, 100.5))
    def test_empty_normal_or_tool_turn_is_not_a_text_completion(self):
        for reason in ["stop", "toolUse"]:
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "session.jsonl"
                source.write_text("\n".join(json.dumps(row) for row in [
                    {"timestamp": 100, "message": {"role": "user", "content": "exact task"}},
                    {"timestamp": 101, "message": {"role": "assistant", "content": [],
                                                "stopReason": reason}},
                ]))
                self.assertIsNone(pi_completion(source, "exact task", 99, 102))
