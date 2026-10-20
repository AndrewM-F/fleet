"""Approved model selection for managed OpenCode workers.

Every provider and model here is a fixture. The tests never reach a provider, never
write an OpenCode database, never start a native session, and never change a hold.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import dispatch, model_selection, opencode, paths, store  # noqa: E402

MINIMAX = "opencode-go/minimax-m3.1-flash-free"
DEEPSEEK = "opencode/deepseek-v4.1-flash-free"
PAID = "opencode/paid-premium-model"
NATIVE_MINIMAX = {"id": "minimax-m3.1-flash-free", "providerID": "opencode-go"}
NATIVE_DEEPSEEK = {"id": "deepseek-v4.1-flash-free", "providerID": "opencode"}


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "config" / "fleet.local.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.native_db = self.root / "opencode.db"
        self.sids = [f"ses_worker_{index}" for index in range(5)]
        self.cwds = {}
        self._write_native()
        self._write_config()
        self.db = store.database(self.data / "fleet.sqlite")
        self.addCleanup(self.db.close)
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DATA", self.data),
                            ("CONFIG", self.config_path),
                            ("DB", self.data / "fleet.sqlite"),
                            ("OPENCODE", self.data / "opencode"),
                            ("LOGS", self.root / "logs")):
            patcher = patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        executable = patch.object(paths, "executable", return_value="/usr/bin/opencode")
        executable.start()
        self.addCleanup(executable.stop)

    # -- fixtures ---------------------------------------------------------
    def _write_native(self):
        with sqlite3.connect(self.native_db) as db:
            db.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, project_id TEXT, "
                       "model TEXT)")
            for sid in self.sids:
                cwd = self.root / ("worker-" + sid)
                cwd.mkdir(parents=True, exist_ok=True)
                self.cwds[sid] = str(cwd)
                db.execute("INSERT INTO session_v2 VALUES (?,?,?,?)",
                           (sid, str(cwd), "project-test",
                            json.dumps(NATIVE_MINIMAX)))
            db.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, "
                       "seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")

    def _write_config(self):
        cfg = {"opencode": [
            {"sessionId": sid, "cwd": self.cwds[sid], "sessionCwd": self.cwds[sid],
             "databasePath": str(self.native_db), "model": MINIMAX,
             "provider": "opencode", "dispatchEnabled": True}
            for sid in self.sids]}
        self.config_path.write_text(json.dumps(cfg, indent=2) + "\n")

    def config(self):
        return json.loads(self.config_path.read_text())

    def slot(self, sid):
        for entry in self.config()["opencode"]:
            if entry["sessionId"] == sid:
                return entry
        raise AssertionError("no slot")

    def native_digest(self):
        return self.native_db.read_bytes()

    def add_task(self, task_id, state="queued", sid=None, message="work"):
        sid = sid or self.sids[0]
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (task_id, sid, self.cwds[sid],
                         json.dumps({"id": task_id, "sessionId": sid, "rootAuthorized": True,
                                     "message": message}), state, 1.0))
        self.db.commit()

    def task_body(self, task_id):
        row = self.db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # -- selection --------------------------------------------------------
    def test_minimax_to_deepseek_for_one_worker(self):
        before = self.native_digest()
        result = model_selection.apply(None, DEEPSEEK, self.sids[0], db=self.db)
        self.assertTrue(result["applied"])
        self.assertEqual(self.slot(self.sids[0])["model"], DEEPSEEK)
        self.assertEqual(self.slot(self.sids[0])["modelSelection"]["model"], DEEPSEEK)
        self.assertEqual(self.slot(self.sids[0])["modelSelection"]["previous"], MINIMAX)
        for sid in self.sids[1:]:
            self.assertEqual(self.slot(sid)["model"], MINIMAX)
            self.assertNotIn("modelSelection", self.slot(sid))
        self.assertEqual(self.native_digest(), before)

    def test_deepseek_to_minimax_reverse(self):
        model_selection.apply(None, DEEPSEEK, self.sids[0], db=self.db)
        result = model_selection.apply(None, MINIMAX, self.sids[0], db=self.db)
        self.assertTrue(result["applied"])
        self.assertEqual(self.slot(self.sids[0])["model"], MINIMAX)
        self.assertEqual(self.slot(self.sids[0])["modelSelection"]["previous"], DEEPSEEK)

    def test_all_workers_selection(self):
        result = model_selection.apply(None, DEEPSEEK, None, db=self.db)
        self.assertTrue(result["applied"])
        self.assertEqual(len(result["changes"]), 5)
        for sid in self.sids:
            self.assertEqual(self.slot(sid)["model"], DEEPSEEK)

    def test_dry_run_reports_the_change_without_writing_it(self):
        before = self.config_path.read_text()
        result = model_selection.apply(None, DEEPSEEK, self.sids[0], dry_run=True, db=self.db)
        self.assertFalse(result["applied"])
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["changes"][0]["action"], "select")
        self.assertEqual(self.config_path.read_text(), before)

    def test_invalid_and_paid_models_are_rejected(self):
        before = self.config_path.read_text()
        for model in (PAID, "not-a-model", "opencode/minimax-m3.1-flash", "",
                      "opencode/", "minimax-m3.1-flash-free"):
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    model_selection.apply(None, model, self.sids[0], db=self.db)
        self.assertEqual(self.config_path.read_text(), before)

    def test_the_allowlist_and_the_legacy_default_are_unchanged(self):
        self.assertEqual(tuple(model_selection.approved_models()),
                         tuple(opencode.APPROVED_MODELS))
        self.assertEqual(model_selection.LEGACY_DEFAULT_MODEL, opencode.MODEL)
        self.assertIn(DEEPSEEK, model_selection.approved_models())
        self.assertIn(MINIMAX, model_selection.approved_models())
        self.assertNotIn(PAID, model_selection.approved_models())

    def test_an_unknown_worker_is_rejected(self):
        with self.assertRaises(ValueError):
            model_selection.apply(None, DEEPSEEK, "ses_unknown", db=self.db)

    # -- active and uncertain workers --------------------------------------
    def test_an_active_worker_keeps_its_in_flight_expectation(self):
        self.add_task("t-running", state="running")
        before = self.config_path.read_text()
        result = model_selection.apply(None, DEEPSEEK, self.sids[0], db=self.db)
        self.assertIn(self.sids[0], result["deferredUntilIdle"])
        # The configured model changes, and the in-flight turn keeps its own model.
        self.assertEqual(self.slot(self.sids[0])["model"], DEEPSEEK)
        self.assertNotEqual(before, self.config_path.read_text())

    def test_an_uncertain_worker_is_reported_busy(self):
        self.add_task("t-uncertain", state="uncertain")
        result = model_selection.apply(None, DEEPSEEK, self.sids[0], db=self.db)
        self.assertIn(self.sids[0], result["deferredUntilIdle"])
        report = model_selection.inspect(self.db)
        worker = [w for w in report["workers"] if w["sessionId"] == self.sids[0]][0]
        self.assertEqual(worker["state"], "active")

    def test_a_permitted_model_set_covers_the_transition(self):
        model_selection.apply(None, DEEPSEEK, self.sids[0], db=self.db)
        slot = self.slot(self.sids[0])
        permitted = opencode.allowed_models(slot)
        self.assertIn(DEEPSEEK, permitted)
        self.assertIn(MINIMAX, permitted)

    # -- the claim and the launch agree -----------------------------------
    def test_the_claim_records_and_launches_the_claimed_model(self):
        model_selection.apply(None, DEEPSEEK, self.sids[0], db=self.db)
        self.add_task("t1", sid=self.sids[0])
        slot = dict(self.slot(self.sids[0]))
        calls = []
        dispatch.dispatch(self.db, {self.sids[0]: slot},
                          {self.sids[0]: {"state": "idle", "cwd": slot["cwd"]}},
                          lambda *args, **rest: calls.append(rest))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], DEEPSEEK)
        self.assertEqual(self.task_body("t1")["model"], DEEPSEEK)

    def test_a_claim_and_its_launch_never_disagree(self):
        self.add_task("t1", sid=self.sids[0])
        slot = dict(self.slot(self.sids[0]))
        calls = []
        dispatch.dispatch(self.db, {self.sids[0]: slot},
                          {self.sids[0]: {"state": "idle", "cwd": slot["cwd"]}},
                          lambda *args, **rest: calls.append(rest))
        claimed = self.task_body("t1")["model"]
        self.assertEqual(calls[0]["model"], claimed)

    def test_a_concurrent_claim_and_switch_cannot_disagree(self):
        self.add_task("t1", sid=self.sids[0])
        slot = dict(self.slot(self.sids[0]))
        calls = []
        started = threading.Event()
        release = threading.Event()

        def slow_sender(*args, **rest):
            started.set()
            release.wait(5)
            calls.append(rest)

        def claim():
            # A claim and a selection are two processes: each has its own
            # database connection, exactly as the Fleet runs them.
            connection = store.database(self.data / "fleet.sqlite")
            try:
                dispatch.dispatch(connection, {self.sids[0]: slot},
                                  {self.sids[0]: {"state": "idle", "cwd": slot["cwd"]}},
                                  slow_sender)
            finally:
                connection.close()

        worker = threading.Thread(target=claim)
        worker.start()
        self.assertTrue(started.wait(5))
        # The claim holds the model lock while it launches, so a selection waits.
        switched = []

        def select():
            connection = store.database(self.data / "fleet.sqlite")
            try:
                switched.append(model_selection.apply(None, DEEPSEEK, self.sids[0],
                                                     db=connection))
            finally:
                connection.close()

        selector = threading.Thread(target=select)
        selector.start()
        time.sleep(0.2)
        self.assertEqual(switched, [], "the selection must wait for the claim")
        release.set()
        worker.join(5)
        selector.join(5)
        self.assertEqual(len(switched), 1)
        self.assertEqual(calls[0]["model"], self.task_body("t1")["model"])

    # -- crash and reload -------------------------------------------------
    def test_a_selection_survives_a_crash_and_reload(self):
        model_selection.apply(None, DEEPSEEK, self.sids[0], db=self.db)
        # A reload re-reads the configuration from disk; nothing is kept in memory.
        reloaded = model_selection.inspect(self.db)
        worker = [w for w in reloaded["workers"] if w["sessionId"] == self.sids[0]][0]
        self.assertEqual(worker["configuredModel"], DEEPSEEK)
        self.assertEqual(worker["selection"]["model"], DEEPSEEK)

    def test_a_reloaded_selection_still_gates_the_next_claim(self):
        model_selection.apply(None, DEEPSEEK, self.sids[0], db=self.db)
        self.add_task("t1", sid=self.sids[0])
        slot = dict(self.slot(self.sids[0]))
        calls = []
        dispatch.dispatch(self.db, {self.sids[0]: slot},
                          {self.sids[0]: {"state": "idle", "cwd": slot["cwd"]}},
                          lambda *args, **rest: calls.append(rest))
        self.assertEqual(calls[0]["model"], DEEPSEEK)

    # -- stale or unexpected native completion ---------------------------
    def test_an_unexpected_native_completion_is_refused_loudly(self):
        # The native completion reports another model: the turn must not be a
        # success, and the refusal must be recorded rather than silent.
        slot = dict(self.slot(self.sids[0]))
        native = opencode.verify_native_session(self.sids[0], slot["cwd"],
                                               str(self.native_db))
        self.assertEqual(native["nativeModelProvider"], "opencode-go")
        self.assertEqual(native["nativeModelId"], "minimax-m3.1-flash-free")
        # A request naming a different model than the native identity, without an
        # explicit selection, is refused before the turn starts.
        with self.assertRaisesRegex(ValueError, "differs from the native session model"):
            opencode.send(slot, "work", "t1", model="opencode/minimax-m3.1-flash-free")

    def test_a_stale_native_identity_is_refused(self):
        # The native row records a model that is not approved at all.
        with sqlite3.connect(self.native_db) as db:
            db.execute("UPDATE session_v2 SET model=? WHERE id=?",
                       (json.dumps({"id": "paid-premium-model", "providerID": "opencode"}),
                        self.sids[0]))
        slot = dict(self.slot(self.sids[0]))
        with self.assertRaises(ValueError):
            opencode.send(slot, "work", "t1", model=MINIMAX)

    # -- nothing else is touched ------------------------------------------
    def test_selection_clears_no_hold_and_keeps_every_other_field(self):
        self.add_task("t-held")
        from fleet import holds

        holds.hold(self.db, "t-held", "operator pause")
        before = self.config()
        model_selection.apply(None, DEEPSEEK, self.sids[0], db=self.db)
        after = self.config()
        for entry in after["opencode"]:
            original = [e for e in before["opencode"]
                        if e["sessionId"] == entry["sessionId"]][0]
            for key in ("sessionId", "cwd", "sessionCwd", "databasePath",
                        "dispatchEnabled"):
                self.assertEqual(entry[key], original[key])
        self.assertTrue(holds.is_held(self.db, "t-held"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM task_holds").fetchone()[0], 1)
        self.assertEqual(self.db.execute(
            "SELECT state FROM tasks WHERE id='t-held'").fetchone()[0], "queued")
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM events WHERE kind='task_held'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
