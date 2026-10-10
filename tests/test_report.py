import json
import sqlite3
import unittest
from fleet.report import report, wait_for_event

class FleetReportTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
        CREATE TABLE events(id INTEGER PRIMARY KEY, kind TEXT, body TEXT, created REAL, acked INTEGER);
        CREATE TABLE tasks(id TEXT PRIMARY KEY, sid TEXT, state TEXT);
        CREATE TABLE snapshots(sid TEXT PRIMARY KEY, body TEXT, observed REAL);
        """)
        self.slots = [{"sessionId": "a", "model": "model", "thinking": "medium", "lane": "one"}]
        self.calls = []
    def tearDown(self):
        self.db.close()
    def event(self, eid, tid, acked=0, kind="review_ready"):
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?)",
            (eid, kind, json.dumps({"sessionId": "a", "taskId": tid}), 10, acked))
    def capture(self, db, eid, body, created):
        self.calls.append(eid)
        return {"summary": "abcdef"}
    def run_report(self, **kw):
        return report(self.db, self.slots, {"tasks": []}, capture=self.capture, now=20, **kw)
    def test_latest_only_keeps_backlog_and_does_not_ack(self):
        self.event(1, "old")
        self.event(2, "new")
        self.event(3, "noise", kind="possibly_stalled")
        result = self.run_report(summary_limit=3)
        worker = result["workers"][0]
        self.assertEqual(self.calls, [2])
        self.assertEqual(worker["pendingReviewCount"], 2)
        self.assertEqual(worker["oldestPendingReviewIds"], [1, 2])
        self.assertEqual(worker["latestCompletion"]["summary"], "abc")
        self.assertTrue(worker["latestCompletion"]["summaryTruncated"])
        self.assertEqual(result["latestEventId"], 3)
        self.assertEqual(self.db.execute("SELECT SUM(acked) FROM events").fetchone()[0], 0)
    def test_after_and_acked_never_recapture(self):
        self.event(1, "old")
        self.run_report(after=1)
        self.assertEqual(self.calls, [])
        self.event(2, "new", acked=1)
        result = self.run_report()
        self.assertEqual(self.calls, [])
        self.assertEqual(result["workers"][0]["pendingReviewCount"], 1)
    def test_queue_and_active_and_import_reserves(self):
        self.db.executemany("INSERT INTO tasks VALUES(?,?,?)",
            [("running", "a", "running"), ("queued", "a", "queued"), ("done", "a", "finished")])
        queue = {"tasks": [{"id": "queued", "sessionId": "a"},
            {"id": "new", "lane": "one"}, {"id": "next", "previousTaskId": "done"}]}
        row = report(self.db, self.slots, queue, capture=self.capture)["workers"][0]
        self.assertEqual(row["queued"], 1)
        self.assertEqual(row["awaitingImport"], 2)
        self.assertEqual(row["activeTasks"], [{"id": "running", "state": "running"}])
    def test_capture_failure_is_bounded_and_report_continues(self):
        self.event(1, "task")
        def fail(*args):
            raise PermissionError("secret diagnostic")
        row = report(self.db, self.slots, {}, capture=fail)["workers"][0]
        self.assertEqual(row["latestCompletion"]["captureError"], "PermissionError")
        self.assertNotIn("secret", json.dumps(row))

    def test_wait_returns_immediately_for_new_completion(self):
        self.event(5, "new")
        sleeps = []
        wait_for_event(self.db, 4, 10, clock=lambda: 0, sleep=sleeps.append)
        self.assertEqual(sleeps, [])
    def test_wait_ignores_noise_and_returns_for_later_completion(self):
        self.event(5, "noise", kind="possibly_stalled")
        sleeps = []
        def sleep(value):
            sleeps.append(value)
            self.event(6, "new")
        wait_for_event(self.db, 4, 10, clock=lambda: 0, sleep=sleep)
        self.assertEqual(sleeps, [2])
    def test_wait_has_bounded_timeout(self):
        ticks = iter([0, 2])
        sleeps = []
        wait_for_event(self.db, 0, 1, clock=lambda: next(ticks), sleep=sleeps.append)
        self.assertEqual(sleeps, [])
