import json
import sqlite3
import unittest

from fleet.report import incremental_report


class IncrementalReportTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE events(id INTEGER PRIMARY KEY, kind TEXT, body TEXT, created REAL, acked INTEGER);
            CREATE TABLE tasks(id TEXT, sid TEXT, state TEXT);
            CREATE TABLE snapshots(sid TEXT, body TEXT, observed REAL);
        """)
        self.slots = [{"sessionId": "worker", "model": "fixture", "effort": "medium"}]
        self.queue = {"tasks": []}

    def tearDown(self):
        self.db.close()

    def event(self, eid, kind="review_ready", acked=0):
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                        (eid, kind, json.dumps({"sessionId": "worker", "taskId": f"task-{eid}"}),
                         float(eid), acked))

    def report(self, **kwargs):
        return incremental_report(self.db, self.slots, self.queue,
                                  capture=kwargs.pop("capture", lambda *_: {"summary": "done"}),
                                  **kwargs)

    def test_pages_multiple_completions_from_same_worker_including_acknowledged(self):
        self.event(1)
        self.event(2, acked=1)
        self.event(3, "heartbeat")
        self.event(4)
        first = self.report(limit=2)
        self.assertEqual([x["eventId"] for x in first["events"]], [1, 2])
        self.assertEqual(first["nextAfter"], 2)
        self.assertTrue(first["hasMore"])
        second = self.report(after=first["nextAfter"], limit=2)
        self.assertEqual([x["eventId"] for x in second["events"]], [4])
        self.assertEqual(second["nextAfter"], 4)
        self.assertFalse(second["hasMore"])
        self.assertEqual(self.db.execute("SELECT acked FROM events WHERE id=1").fetchone()[0], 0)

    def test_event_arriving_during_capture_is_not_skipped(self):
        self.event(1)
        def capture(*_):
            self.event(2)
            return {"summary": "first"}
        first = self.report(capture=capture)
        self.assertEqual(first["nextAfter"], 1)
        second = self.report(after=first["nextAfter"])
        self.assertEqual([x["eventId"] for x in second["events"]], [2])

    def test_errors_and_unrostered_workers_are_not_dropped(self):
        self.event(1, "runner_error")
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                        (2, "review_ready", json.dumps({"sessionId": "retired", "taskId": "old"}),
                         2.0, 0))
        result = self.report()
        self.assertEqual([x["kind"] for x in result["events"]], ["runner_error", "review_ready"])
        self.assertEqual(result["events"][1]["sessionId"], "retired")

    def test_capture_failure_is_explicit_and_summary_is_bounded(self):
        self.event(1)
        result = self.report(summary_limit=2, capture=lambda *_: {"summary": "abcdef"})
        self.assertEqual(result["events"][0]["summary"], "ab")
        self.assertTrue(result["events"][0]["summaryTruncated"])
        def broken(*_):
            raise RuntimeError("do not copy raw payload")
        result = self.report(capture=broken)
        self.assertEqual(result["events"][0]["captureError"], "RuntimeError")
        self.assertNotIn("do not copy", json.dumps(result))

    def test_cursor_never_rewinds_and_bounds_are_validated(self):
        self.event(1, "heartbeat")
        self.assertEqual(self.report(after=4)["nextAfter"], 4)
        for args in ({"after": -1}, {"limit": 0}, {"limit": 51}, {"summary_limit": -1}):
            with self.assertRaises(ValueError):
                self.report(**args)


if __name__ == "__main__":
    unittest.main()
