import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fleet import pr_queue as planner

HEAD = "a" * 40

def pr(**changes):
    value = {"number": 1, "state": "OPEN", "headRefOid": HEAD, "isDraft": True,
             "mergeStateStatus": "CLEAN", "statusCheckRollup": [
                 {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
    value.update(changes)
    return value

class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY,sid TEXT,cwd TEXT,body TEXT,state TEXT,updated REAL)")
        self.mission = {"repo": "owner/repo", "pr": 1, "sessionId": "s", "rootAuthorized": True}
        self.slots = {"s": {"sessionId": "s", "cwd": str(self.root)}}
        (self.root / "pr-missions.json").write_text(json.dumps(
            {"author": "root", "enabled": True, "missions": [self.mission]}))
        planner._next_poll = 0
        planner._repo_index = 0

    def run_tick(self, observation, state="idle", now=1000):
        with patch.object(planner.paths, "DATA", self.root), \
             patch.object(planner.paths, "PAUSE", self.root / "paused"), \
             patch.object(planner, "fetch", return_value={1: observation}), \
             patch.object(planner, "emit") as emit, \
             patch.object(planner, "enqueue", return_value=[{"alreadyQueued": False}]) as enqueue:
            planner.tick(self.db, self.slots, {"s": {"state": state}}, now=now)
            return enqueue, emit

    def test_exact_head_is_required_for_merge(self):
        self.assertEqual(planner.classify(pr(), "b" * 40)[0], "needs_review")
        self.assertEqual(planner.classify(pr(), HEAD)[0], "merge")

    def test_pending_and_missing_checks_cannot_merge(self):
        self.assertEqual(planner.classify(pr(statusCheckRollup=[]), HEAD)[0], "finish_draft")
        checks = [{"name": "test", "status": "IN_PROGRESS", "conclusion": None}]
        self.assertEqual(planner.classify(pr(statusCheckRollup=checks), HEAD)[0], "waiting_ci")

    def test_failure_takes_priority_over_review(self):
        checks = [{"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assertEqual(planner.classify(pr(statusCheckRollup=checks), HEAD)[0], "repair_ci")

    def test_cancelled_draft_ci_is_not_a_code_failure(self):
        checks = [{"name": "validation", "status": "COMPLETED", "conclusion": "CANCELLED"}]
        self.assertEqual(planner.classify(pr(statusCheckRollup=checks))[0], "needs_review")

    def test_cancelled_ready_ci_cannot_merge(self):
        checks = [{"name": "validation", "status": "COMPLETED", "conclusion": "CANCELLED"}]
        self.assertEqual(planner.classify(pr(isDraft=False, statusCheckRollup=checks), HEAD)[0], "needs_checks")

    def test_unique_owners_and_explicit_scope(self):
        config = {"author": "root", "enabled": True, "missions": [self.mission, self.mission]}
        with self.assertRaises(ValueError):
            planner.validate(config, self.slots)
        self.assertEqual(planner.validate(dict(config, enabled=False), self.slots), [])

    def test_running_worker_not_interrupted(self):
        enqueue, _ = self.run_tick(pr(mergeStateStatus="DIRTY"), state="working")
        enqueue.assert_not_called()

    def test_uncertain_task_blocks_successor(self):
        self.db.execute("INSERT INTO tasks VALUES ('old','s','','{}','uncertain',0)")
        enqueue, _ = self.run_tick(pr(mergeStateStatus="DIRTY"))
        enqueue.assert_not_called()

    def test_finished_task_allows_distinct_successor(self):
        self.db.execute("INSERT INTO tasks VALUES ('old','s','','{}','finished',0)")
        enqueue, _ = self.run_tick(pr(mergeStateStatus="DIRTY"))
        self.assertEqual(enqueue.call_count, 1)

    def test_completed_identical_mission_is_not_repeated(self):
        value = pr(mergeStateStatus="DIRTY")
        task = planner.task_for(self.mission, value, "repair_conflicts", [])
        self.db.execute("INSERT INTO tasks VALUES (?, 's','','{}','finished',0)", (task["id"],))
        enqueue, emit = self.run_tick(value)
        enqueue.assert_not_called()
        self.assertTrue(any(c.args[1] == "pr_needs_root" for c in emit.call_args_list))

    def test_successor_requires_verified_merge(self):
        mission = dict(self.mission, nextTask={"id": "next", "message": "Approved milestone task",
                                            "rootAuthorized": True})
        self.assertIsNone(planner.task_for(mission, pr(), "needs_review", []))
        task = planner.task_for(mission, pr(state="MERGED"), "merged", [])
        self.assertEqual(task["id"], "next")
        self.assertIsNone(planner.task_for(self.mission, pr(state="MERGED"), "merged", []))

    def test_pause_stops_poll_and_dispatch(self):
        (self.root / "paused").touch()
        enqueue, emit = self.run_tick(pr(mergeStateStatus="DIRTY"))
        enqueue.assert_not_called()
        emit.assert_not_called()

    def test_poll_is_rate_limited(self):
        self.run_tick(pr(mergeStateStatus="DIRTY"))
        enqueue, emit = self.run_tick(pr(mergeStateStatus="DIRTY"), now=1001)
        enqueue.assert_not_called()
        emit.assert_not_called()

    def test_task_id_survives_restart(self):
        value = pr(mergeStateStatus="DIRTY")
        first = planner.task_for(self.mission, value, "repair_conflicts", [])
        second = planner.task_for(self.mission, value, "repair_conflicts", [])
        self.assertEqual(first, second)

    def test_closed_pr_does_not_schedule_milestone(self):
        self.assertEqual(planner.classify(pr(state="CLOSED"))[0], "closed")
        self.assertIsNone(planner.task_for(self.mission, pr(state="CLOSED"), "closed", []))

    def test_fetch_failure_is_visible_without_dispatch(self):
        with patch.object(planner.paths, "DATA", self.root), \
             patch.object(planner.paths, "PAUSE", self.root / "paused"), \
             patch.object(planner, "fetch", side_effect=OSError("private details")), \
             patch.object(planner, "emit") as emit, patch.object(planner, "enqueue") as enqueue:
            planner.tick(self.db, self.slots, {"s": {"state": "idle"}}, now=1000)
            enqueue.assert_not_called()
            self.assertEqual(emit.call_args.kwargs, {"errorType": "OSError"})

    def test_fetch_limits_rollups_to_open_prs_and_reads_missing_owned_pr(self):
        from types import SimpleNamespace
        open_pr = pr()
        merged_pr = pr(number=2, state="MERGED")
        responses = [SimpleNamespace(stdout=json.dumps([open_pr])),
                     SimpleNamespace(stdout=json.dumps(merged_pr))]
        with patch.object(planner.subprocess, "run", side_effect=responses) as run:
            result = planner.fetch("owner/repo", [1, 2])
        self.assertEqual(result, {1: open_pr, 2: merged_pr})
        first = run.call_args_list[0].args[0]
        self.assertEqual(first[first.index("--state") + 1], "open")
        self.assertEqual(first[first.index("--limit") + 1], "30")
        self.assertEqual(run.call_args_list[1].args[0][:4], ["gh", "pr", "view", "2"])

    def test_fetch_rejects_mismatched_owned_pr(self):
        from types import SimpleNamespace
        with patch.object(planner.subprocess, "run", side_effect=[
            SimpleNamespace(stdout="[]"),
            SimpleNamespace(stdout=json.dumps(pr(number=99))),
        ]):
            with self.assertRaises(ValueError):
                planner.fetch("owner/repo", [2])

    def test_fetch_does_not_guess_missing_pr_is_merged_on_error(self):
        from types import SimpleNamespace
        with patch.object(planner.subprocess, "run", side_effect=[
            SimpleNamespace(stdout="[]"), OSError("unavailable"),
        ]):
            with self.assertRaises(OSError):
                planner.fetch("owner/repo", [2])

if __name__ == "__main__":
    unittest.main()
