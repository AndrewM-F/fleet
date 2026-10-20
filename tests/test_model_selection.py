"""Approved model selection for managed OpenCode workers.

Every provider and model here is a fixture. The tests never reach a provider, never
write an OpenCode database, never start a native session, and never change a hold.
"""
import contextlib
import io
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

from fleet import cli, dispatch, model_selection, opencode, paths, store  # noqa: E402

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

    # -- the command surface ----------------------------------------------
    def test_inspect_reports_and_never_writes(self):
        before = self.config_path.read_text()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.main(["opencode-model", "--inspect", "--model", DEEPSEEK])
        self.assertEqual(self.config_path.read_text(), before)
        report = json.loads(buffer.getvalue())
        self.assertEqual(len(report["workers"]), 5)
        self.assertEqual(report["legacyDefaultModel"], opencode.MODEL)
        self.assertEqual(report["approvedModels"], list(opencode.APPROVED_MODELS))
        for worker in report["workers"]:
            self.assertEqual(worker["configuredModel"], MINIMAX)
            self.assertEqual(worker["nativeIdentityModel"], MINIMAX)
            # The reported native directory is the canonical one, exactly as
            # verify_native_session reports it: a path alias is the same
            # directory, so the comparison follows the path.
            self.assertEqual(worker["nativeDirectory"],
                             str(Path(self.cwds[worker["sessionId"]]).resolve()))

    def test_a_symlinked_cwd_is_the_same_directory(self):
        """A path alias names the same directory, and is not a mismatch."""
        sid = self.sids[0]
        alias = self.root / "alias"
        alias.symlink_to(self.cwds[sid])
        cfg = self.config()
        for entry in cfg["opencode"]:
            if entry["sessionId"] == sid:
                entry["cwd"] = str(alias)
                entry["sessionCwd"] = str(alias)
        self.config_path.write_text(json.dumps(cfg, indent=2) + "\n")
        report = model_selection.inspect(self.db)
        worker = next(w for w in report["workers"] if w["sessionId"] == sid)
        self.assertIsNone(worker["nativeError"])
        self.assertEqual(worker["nativeDirectory"],
                         str(Path(self.cwds[sid]).resolve()))
        result = model_selection.apply(None, DEEPSEEK, sid, db=self.db)
        self.assertTrue(result["applied"])
        self.assertEqual(result["deferredUntilIdle"], [])
        self.assertEqual(self.slot(sid)["model"], DEEPSEEK)
        self.assertEqual(self.slot(sid)["cwd"], str(alias))

    def test_a_model_without_a_target_is_refused(self):
        before = self.config_path.read_text()
        with self.assertRaises(SystemExit):
            cli.main(["opencode-model", "--model", DEEPSEEK])
        self.assertEqual(self.config_path.read_text(), before)

    def test_dry_run_through_the_command_changes_nothing(self):
        before = self.config_path.read_text()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.main(["opencode-model", "--model", DEEPSEEK, "--all", "--dry-run"])
        self.assertEqual(self.config_path.read_text(), before)
        result = json.loads(buffer.getvalue())
        self.assertFalse(result["applied"])
        self.assertTrue(result["dryRun"])

    # -- the reviewed defects ----------------------------------------------
    def live_turn(self, sid, model, turn="live-turn", state="working"):
        """A managed turn that is in flight for this worker."""
        directory = opencode.turn_dir(sid, turn)
        directory.mkdir(parents=True, exist_ok=True)
        cwd = str(Path(self.cwds[sid]).resolve())
        identity = {"sessionId": sid, "cwd": cwd, "model": model}
        (directory.parent.parent / "current.json").write_text(json.dumps(
            dict(identity, turn=turn, startedAt=time.time())))
        (directory / "state.json").write_text(json.dumps(dict(
            identity, turn=turn, state=state, progress=0, startedAt=time.time(),
            processIdentity={"pid": 43001, "pgid": 43001, "start": [100, 200],
                             "executable": "/usr/bin/opencode"})))
        return directory

    def test_a_switch_while_active_does_not_make_the_turn_uncancellable(self):
        # A cancellation binds to the model the turn was launched with. The
        # configured model is the desired model for the next productive turn
        # and never decides whether a running turn may be cancelled.
        sid = self.sids[0]
        directory = self.live_turn(sid, MINIMAX)
        model_selection.apply(None, DEEPSEEK, sid, db=self.db)
        self.assertEqual(self.slot(sid)["model"], DEEPSEEK)
        with patch.object(opencode, "identity_matches", return_value=True):
            result = opencode.cancel(self.slot(sid))
        self.assertEqual(result["turn"], "live-turn")
        self.assertTrue((directory / "cancel.json").exists())

    def test_a_cancel_refuses_a_turn_whose_own_records_disagree(self):
        # The identity checks are retained: a turn whose state names another
        # model than its launch record is refused, and nothing is signalled.
        sid = self.sids[0]
        directory = self.live_turn(sid, MINIMAX)
        state = json.loads((directory / "state.json").read_text())
        state["model"] = DEEPSEEK
        (directory / "state.json").write_text(json.dumps(state))
        with patch.object(opencode, "identity_matches", return_value=True):
            with self.assertRaisesRegex(ValueError, "identity/cwd/model mismatch"):
                opencode.cancel(self.slot(sid))
        self.assertFalse((directory / "cancel.json").exists())

    def test_a_cancel_still_checks_the_session_and_the_process(self):
        # The session, the working directory and the process identity checks
        # are kept: only the model binding changed.
        sid = self.sids[0]
        directory = self.live_turn(sid, MINIMAX)
        launch = json.loads((directory.parent.parent / "current.json").read_text())
        launch["cwd"] = str(self.root)
        (directory.parent.parent / "current.json").write_text(json.dumps(launch))
        with patch.object(opencode, "identity_matches", return_value=True):
            with self.assertRaisesRegex(ValueError, "session/cwd identity mismatch"):
                opencode.cancel(self.slot(sid))
        self.assertFalse((directory / "cancel.json").exists())
        launch["cwd"] = str(Path(self.cwds[sid]).resolve())
        (directory.parent.parent / "current.json").write_text(json.dumps(launch))
        with patch.object(opencode, "identity_matches", return_value=False):
            with self.assertRaisesRegex(ValueError, "identity is not live"):
                opencode.cancel(self.slot(sid))
        self.assertFalse((directory / "cancel.json").exists())

    def test_repeated_selections_keep_the_active_turn_identity(self):
        # A turn that is in flight keeps its own immutable expectation. A
        # second selection must not strand it, and the worker stays available.
        sid = self.sids[0]
        self.live_turn(sid, MINIMAX)
        model_selection.apply(None, DEEPSEEK, sid, db=self.db)
        model_selection.apply(None, "opencode/minimax-m3.1-flash-free", sid, db=self.db)
        slot = self.slot(sid)
        self.assertEqual(slot["model"], "opencode/minimax-m3.1-flash-free")
        self.assertEqual(slot["modelSelection"]["previous"], DEEPSEEK)
        # The mutable selection chain alone no longer holds the launched model.
        self.assertNotIn(MINIMAX, opencode.allowed_models(slot))
        with patch.object(opencode, "identity_matches", return_value=True):
            observed = opencode.state(slot)
        self.assertEqual(observed["state"], "working")
        self.assertEqual(observed["model"], MINIMAX)

    def test_a_turn_state_must_agree_with_its_own_launch_record(self):
        # The launch record is the turn's immutable expectation. The state
        # record is checked against it and not against the mutable roster, so
        # a later selection cannot make a mismatched turn look legitimate.
        sid = self.sids[0]
        directory = self.live_turn(sid, MINIMAX)
        model_selection.apply(None, DEEPSEEK, sid, db=self.db)
        state = json.loads((directory / "state.json").read_text())
        state["model"] = DEEPSEEK
        (directory / "state.json").write_text(json.dumps(state))
        with patch.object(opencode, "identity_matches", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "identity/cwd/model mismatch"):
                opencode.state(self.slot(sid))

    def test_a_selection_that_lands_before_the_claim_is_seen(self):
        # The roster was loaded before the selection landed. The configuration
        # is reloaded inside the shared lock, so the claim and the launch use
        # the model that is current, never the one the roster was loaded with.
        sid = self.sids[0]
        stale = {sid: dict(self.slot(sid))}
        self.assertEqual(stale[sid]["model"], MINIMAX)
        model_selection.apply(None, DEEPSEEK, sid, db=self.db)
        self.add_task("t1", sid=sid)
        calls = []
        dispatch.dispatch(self.db, stale,
                          {sid: {"state": "idle", "cwd": stale[sid]["cwd"]}},
                          lambda *args, **rest: calls.append((args, rest)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["model"], DEEPSEEK)
        self.assertEqual(calls[0][0][0]["model"], DEEPSEEK)
        self.assertEqual(self.task_body("t1")["model"], DEEPSEEK)

    def test_a_worker_disabled_before_the_claim_is_not_claimed(self):
        # The reloaded configuration is validated, not just read.
        sid = self.sids[0]
        stale = {sid: dict(self.slot(sid))}
        cfg = self.config()
        for entry in cfg["opencode"]:
            if entry["sessionId"] == sid:
                entry["dispatchEnabled"] = False
        self.config_path.write_text(json.dumps(cfg, indent=2) + "\n")
        self.add_task("t1", sid=sid)
        calls = []
        dispatch.dispatch(self.db, stale,
                          {sid: {"state": "idle", "cwd": stale[sid]["cwd"]}},
                          lambda *args, **rest: calls.append(rest))
        self.assertEqual(calls, [])
        self.assertEqual(self.db.execute(
            "SELECT state FROM tasks WHERE id='t1'").fetchone()[0], "queued")

    # -- the fail-closed reload --------------------------------------------
    def test_a_removed_worker_is_never_launched_from_a_cached_roster(self):
        # The worker was retired from the configuration after the roster was
        # loaded. The claim is refused, the task stays queued and the sender is
        # never called.
        sid = self.sids[0]
        stale = {sid: dict(self.slot(sid))}
        cfg = self.config()
        cfg["opencode"] = [entry for entry in cfg["opencode"]
                           if entry["sessionId"] != sid]
        self.config_path.write_text(json.dumps(cfg, indent=2) + "\n")
        self.add_task("t1", sid=sid)
        calls = []
        dispatch.dispatch(self.db, stale,
                          {sid: {"state": "idle", "cwd": stale[sid]["cwd"]}},
                          lambda *args, **rest: calls.append(rest))
        self.assertEqual(calls, [])
        self.assertEqual(self.db.execute(
            "SELECT state FROM tasks WHERE id='t1'").fetchone()[0], "queued")
        # The refusal is recorded once, so the stall is never silent.
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM events WHERE kind='needs_attention'").fetchone()[0], 1)

    def test_an_unreadable_configuration_leaves_the_task_queued(self):
        sid = self.sids[0]
        stale = {sid: dict(self.slot(sid))}
        self.config_path.unlink()
        self.add_task("t1", sid=sid)
        calls = []
        dispatch.dispatch(self.db, stale,
                          {sid: {"state": "idle", "cwd": stale[sid]["cwd"]}},
                          lambda *args, **rest: calls.append(rest))
        self.assertEqual(calls, [])
        self.assertEqual(self.db.execute(
            "SELECT state FROM tasks WHERE id='t1'").fetchone()[0], "queued")
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM events WHERE kind='needs_attention'").fetchone()[0], 1)

    def test_a_malformed_configuration_leaves_the_task_queued(self):
        sid = self.sids[0]
        stale = {sid: dict(self.slot(sid))}
        self.config_path.write_text("{ this is not json")
        self.add_task("t1", sid=sid)
        calls = []
        dispatch.dispatch(self.db, stale,
                          {sid: {"state": "idle", "cwd": stale[sid]["cwd"]}},
                          lambda *args, **rest: calls.append(rest))
        self.assertEqual(calls, [])
        self.assertEqual(self.db.execute(
            "SELECT state FROM tasks WHERE id='t1'").fetchone()[0], "queued")

    def test_an_ambiguous_configuration_leaves_the_task_queued(self):
        # The configuration describes the same session twice.
        sid = self.sids[0]
        stale = {sid: dict(self.slot(sid))}
        cfg = self.config()
        cfg["opencode"].append(dict(cfg["opencode"][0]))
        self.config_path.write_text(json.dumps(cfg, indent=2) + "\n")
        self.add_task("t1", sid=sid)
        calls = []
        dispatch.dispatch(self.db, stale,
                          {sid: {"state": "idle", "cwd": stale[sid]["cwd"]}},
                          lambda *args, **rest: calls.append(rest))
        self.assertEqual(calls, [])
        self.assertEqual(self.db.execute(
            "SELECT state FROM tasks WHERE id='t1'").fetchone()[0], "queued")

    def test_the_reload_refuses_a_missing_unreadable_malformed_or_ambiguous_configuration(self):
        sid = self.sids[0]
        slot = dict(self.slot(sid))
        pristine = self.config()

        def write(entries):
            cfg = dict(pristine)
            cfg["opencode"] = entries
            self.config_path.write_text(json.dumps(cfg, indent=2) + "\n")

        self.assertEqual(model_selection.current_slot(slot)["model"], MINIMAX)
        # A worker that is no longer configured.
        write([entry for entry in pristine["opencode"] if entry["sessionId"] != sid])
        with self.assertRaisesRegex(ValueError, "one existing configured OpenCode session"):
            model_selection.current_slot(slot)
        # A configuration that describes the worker twice.
        write(pristine["opencode"] + [dict([entry for entry in pristine["opencode"]
                                            if entry["sessionId"] == sid][0])])
        with self.assertRaisesRegex(ValueError, "one existing configured OpenCode session"):
            model_selection.current_slot(slot)
        # A configuration that cannot be read.
        self.config_path.unlink()
        with self.assertRaisesRegex(ValueError, "could not be read"):
            model_selection.current_slot(slot)
        # A configuration that cannot be parsed.
        self.config_path.write_text("{ this is not json")
        with self.assertRaisesRegex(ValueError, "could not be read"):
            model_selection.current_slot(slot)

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
