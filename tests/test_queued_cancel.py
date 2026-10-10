import sqlite3
import unittest
from unittest.mock import patch
from fleet import ops

class QueuedCancellationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, sid TEXT, state TEXT, updated REAL)")
        self.db.execute("INSERT INTO tasks VALUES ('task', 'worker', 'queued', 0)")
        self.db.commit()
        self.addCleanup(self.db.close)

    def test_queued_cancel_never_touches_worker(self):
        with patch.object(ops, "emit") as event, patch.object(ops.paths, "slots") as slots, patch.object(ops.pi, "send") as send:
            result = ops.cancel(self.db, "task", "superseded")
        self.assertEqual(result["action"], {"queuedCancelled": True})
        self.assertEqual(self.db.execute("SELECT state FROM tasks").fetchone()[0], "cancelled")
        event.assert_called_once_with(self.db, "task_cancelled", taskId="task", sessionId="worker", note="superseded")
        slots.assert_not_called()
        send.assert_not_called()

    def test_duplicate_cancel_cannot_abort_new_work(self):
        with patch.object(ops, "emit"), patch.object(ops.pi, "send") as send:
            ops.cancel(self.db, "task", "superseded")
            with self.assertRaises(ValueError):
                ops.cancel(self.db, "task", "duplicate")
        send.assert_not_called()

    def test_finished_task_is_unchanged(self):
        self.db.execute("UPDATE tasks SET state='finished'")
        self.db.commit()
        with self.assertRaises(ValueError):
            ops.cancel(self.db, "task", "do not change")
        self.assertEqual(self.db.execute("SELECT state FROM tasks").fetchone()[0], "finished")

    def test_missing_task_is_rejected(self):
        with self.assertRaises(ValueError):
            ops.cancel(self.db, "missing", "missing")

    def test_lost_queued_claim_is_not_cancelled(self):
        underlying = self.db
        class RacingDatabase:
            def execute(self, sql, args=()):
                if sql.startswith("UPDATE tasks SET state='cancelled'"):
                    underlying.execute("UPDATE tasks SET state='sent'")
                    underlying.commit()
                return underlying.execute(sql, args)
            def rollback(self): underlying.rollback()
            def commit(self): underlying.commit()
        with patch.object(ops, "emit") as event, patch.object(ops.pi, "send") as send:
            with self.assertRaisesRegex(ValueError, "Task state changed"):
                ops.cancel(RacingDatabase(), "task", "raced")
        self.assertEqual(underlying.execute("SELECT state FROM tasks").fetchone()[0], "sent")
        event.assert_not_called()
        send.assert_not_called()
