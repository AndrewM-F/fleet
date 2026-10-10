"""Synthetic queue-starvation and completion-to-successor dispatch checks."""
import json
from pathlib import Path
import tempfile
import time
import unittest

from fleet import dispatch, manager, store, watchdog


class QueueWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = str(Path(self.tmp.name).resolve())
        self.db = store.database(Path(self.cwd) / "test.sqlite")
        self.slots = {"s": {"sessionId": "s", "provider": "pi", "cwd": self.cwd}}
        self.states = {"s": {"state": "idle", "cwd": self.cwd, "progress": "new"}}
        self.now = time.time()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def runway(self, queued=0, active=0, uncertain=0, awaiting=0):
        return {"workers": [{"sessionId": "s", "queued": queued, "active": active,
                             "uncertain": uncertain, "awaitingImport": awaiting}]}

    def check(self, **kwargs):
        return watchdog.check_queue(self.db, self.slots, self.states,
                                    now=kwargs.pop("now", self.now),
                                    runway=kwargs.pop("runway", self.runway()), **kwargs)

    def task(self, tid, state, updated=None):
        body = {"id": tid, "sessionId": "s", "message": tid,
                "rootAuthorized": True, "baselineProgress": "old"}
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (tid, "s", self.cwd, json.dumps(body), state,
                         self.now if updated is None else updated))
        self.db.commit()

    def test_empty_queue_alert_is_durable_and_deduplicated(self):
        self.assertEqual(len(self.check()), 1)
        self.assertEqual(self.check(), [])
        row = self.db.execute("SELECT kind,body FROM events").fetchone()
        self.assertEqual(row[0], "needs_attention")
        self.assertTrue(json.loads(row[1])["workers"][0]["idle"])
        # A later invocation has no in-memory episode state.
        self.assertEqual(len(self.check(now=self.now + 601)), 1)

    def test_busy_worker_without_reserve_warns_before_idle(self):
        self.states["s"]["state"] = "working"
        self.check(runway=self.runway(active=1))
        body = json.loads(self.db.execute("SELECT body FROM events").fetchone()[0])
        self.assertFalse(body["workers"][0]["idle"])

    def test_pending_import_counts_as_reserve_and_recovery_rearms_alert(self):
        self.check()
        recovered = self.check(runway=self.runway(awaiting=1))
        self.assertEqual(len(recovered), 1)
        kind = self.db.execute("SELECT kind FROM events ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(kind, "state_changed")
        self.assertEqual(len(self.check()), 1)

    def test_paused_disabled_uncertain_and_unknown_workers_are_not_refilled(self):
        self.assertEqual(self.check(paused=True), [])
        self.slots["s"].update(provider="claude", dispatchEnabled=False)
        self.assertEqual(self.check(), [])
        self.slots["s"]["dispatchEnabled"] = True
        self.assertEqual(self.check(runway=self.runway(uncertain=1)), [])
        self.states["s"]["state"] = "unknown"
        self.assertEqual(self.check(), [])

    def test_old_queued_work_on_idle_worker_reports_dispatch_block(self):
        self.task("next", "queued", self.now - 121)
        self.check(runway=self.runway(queued=1))
        body = json.loads(self.db.execute("SELECT body FROM events").fetchone()[0])
        self.assertEqual(body["category"], "dispatch_blocked")
        self.assertEqual(body["workers"], [{"sessionId": "s", "taskId": "next"}])
        self.assertEqual(self.check(runway=self.runway(queued=1)), [])

    def test_recent_queued_work_is_not_a_dispatch_failure(self):
        self.task("next", "queued")
        self.assertEqual(self.check(runway=self.runway(queued=1)), [])

    def test_completion_dispatches_preapproved_successor_once(self):
        self.task("first", "running")
        self.task("second", "queued")
        manager.observe(self.db, "s", self.states["s"], now=self.now)
        sent = []
        dispatch.dispatch(self.db, self.slots, self.states,
                          lambda slot, text: sent.append(text))
        dispatch.dispatch(self.db, self.slots, self.states,
                          lambda slot, text: sent.append(text))
        self.assertEqual(sent, ["second"])
        self.assertEqual(dict(self.db.execute("SELECT id,state FROM tasks")),
                         {"first": "finished", "second": "sent"})
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM events WHERE kind='review_ready'").fetchone()[0], 1)

    def test_uncertain_predecessor_blocks_successor_and_pause_is_preserved(self):
        self.task("first", "uncertain")
        self.task("second", "queued")
        sent = []
        dispatch.dispatch(self.db, self.slots, self.states,
                          lambda slot, text: sent.append(text))
        self.assertEqual(sent, [])
        self.db.execute("UPDATE tasks SET state='finished' WHERE id='first'")
        self.db.commit()
        dispatch.dispatch(self.db, self.slots, self.states,
                          lambda slot, text: sent.append(text), paused=True)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
