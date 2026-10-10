import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fleet import control


class QueueRunwayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.queue = Path(self.temp.name) / "queue.json"
        self.queue.write_text(json.dumps({"author": "root", "tasks": []}))
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE tasks (id TEXT, sid TEXT, state TEXT)")
        self.db.execute("CREATE TABLE events (id INTEGER, acked INTEGER)")
        self.patches = [
            patch.object(control.paths, "QUEUE", self.queue),
            patch.object(control.paths, "slots",
                         return_value=([{"sessionId": "a"}], [{"sessionId": "b"}])),
            patch.object(control, "database", return_value=self.db),
            patch.object(control.events, "pending", return_value=[]),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.db.close()
        self.temp.cleanup()

    def add(self, task_id, sid, state):
        self.db.execute("INSERT INTO tasks VALUES (?,?,?)", (task_id, sid, state))
        self.db.commit()

    def test_active_is_not_reserve(self):
        self.add("a1", "a", "running")
        self.add("b1", "b", "sent")
        report = control.queue_runway(self.db)
        self.assertEqual(report["needsTasks"], ["a", "b"])
        self.assertEqual([w["active"] for w in report["workers"]], [1, 1])

    def test_queued_successors_cover_both_workers(self):
        self.add("a1", "a", "queued")
        self.add("b1", "b", "queued")
        self.assertEqual(control.queue_runway(self.db)["needsTasks"], [])

    def test_uncertain_and_finished_never_count(self):
        self.add("a1", "a", "uncertain")
        self.add("b1", "b", "finished")
        report = control.queue_runway(self.db)
        self.assertEqual(report["needsTasks"], ["a", "b"])
        self.assertEqual(report["workers"][0]["uncertain"], 1)

    def test_pending_import_is_distinct_and_deduplicated(self):
        task = {"id": "new", "sessionId": "a", "rootAuthorized": True, "message": "work"}
        self.queue.write_text(json.dumps({"author": "root", "tasks": [task, task]}))
        report = control.queue_runway(self.db)
        self.assertEqual(report["needsTasks"], ["b"])
        self.assertEqual(report["workers"][0]["awaitingImport"], 1)
        self.assertEqual(report["workers"][0]["queued"], 0)
        self.add("new", "a", "finished")
        self.assertEqual(control.queue_runway(self.db)["needsTasks"], ["a", "b"])

    def test_unapproved_or_unknown_targets_do_not_cover_workers(self):
        self.queue.write_text(json.dumps({"author": "root", "tasks": [
            {"id": "x", "sessionId": "a", "message": "work"},
            {"id": "y", "sessionId": "other", "rootAuthorized": True, "message": "work"},
        ]}))
        self.assertEqual(control.queue_runway(self.db)["needsTasks"], ["a", "b"])

    def test_cycle_returns_early_when_runway_is_low(self):
        result = control.cycle({"minQueued": 1, "timeout": 60})
        self.assertTrue(result["queueLow"])
        self.assertFalse(result["timeout"])
        self.assertEqual(result["runway"]["needsTasks"], ["a", "b"])

    def test_default_remains_nonblocking_snapshot_with_timeout_zero(self):
        result = control.cycle({"timeout": 0})
        self.assertFalse(result["queueLow"])
        self.assertFalse(result["timeout"])
        self.assertEqual(len(result["runway"]["workers"]), 2)

    def test_minimum_rejects_bool_and_out_of_range(self):
        for minimum in (True, -1, 11, "1"):
            with self.assertRaisesRegex(ValueError, "Invalid queue minimum"):
                control.cycle({"minQueued": minimum})


if __name__ == "__main__":
    unittest.main()
