"""Offline disposable qualification: the fake owns no native account/process."""
import io
import json
import os
from contextlib import nullcontext
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fleet import disposable


class FakeExecutor:
    def __init__(self):
        self.condition = threading.Condition()
        self.created = []
        self.ran = []
        self.retired = []
        self.running = 0
        self.peak = 0
        self.release = threading.Event()
        self.create_release = threading.Event()
        self.create_release.set()
        self.retire_release = threading.Event()
        self.retire_release.set()
        self.create_error = False
        self.run_error = False
        self.retire_error = False
        self.reuse = False
        self.state = "finished"
        self.stopped = True
        self.allowed = True
        self.expire_after_create = False
        self.bindings = []

    def verify(self, config):
        if not self.allowed:
            raise disposable.AdmissionRefused("Exact account eligibility proof unavailable")
        self.bindings.append(config)

    def verify_clean_start(self, config):
        return True

    def create(self, config, job):
        with self.condition:
            self.created.append(job.id)
            sid = "ses_1" if self.reuse else "ses_" + str(len(self.created))
            self.condition.notify_all()
        self.create_release.wait()
        if self.create_error:
            raise RuntimeError("Ambiguous create outcome")
        if self.expire_after_create:
            self.allowed = False
        return sid

    def run(self, config, sid, job, cancel):
        with self.condition:
            self.ran.append((sid, job.id, job.message))
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.condition.notify_all()
        while not self.release.wait(.01):
            if cancel.is_set():
                break
        with self.condition:
            self.running -= 1
        if self.run_error:
            raise RuntimeError("Native stop unknown")
        return disposable.RunResult(self.state, "result " + job.id, self.stopped)

    def retire(self, config, sid):
        with self.condition:
            self.retired.append(sid)
            self.condition.notify_all()
        self.retire_release.wait()
        if self.retire_error:
            raise RuntimeError("Delete unavailable")
        return True

    def wait_for(self, predicate):
        with self.condition:
            if not self.condition.wait_for(predicate, timeout=3):
                raise AssertionError("Fake lifecycle did not reach expected boundary")


def task(identifier):
    return {"id": identifier, "message": "Do " + identifier, "rootAuthorized": True}


class DisposableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = str(Path(self.tmp.name).resolve())
        self.raw = {"executable": root + "/opencode", "account": "existing-account:provider-selection",
                    "cwd": root, "model": disposable.MODEL, "maxConcurrent": 3}
        self.config = disposable.Config.parse(self.raw)
        self.fake = FakeExecutor()
        self.fleet = disposable.Fleet(self.config, self.fake)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.fake.create_release.set()
        self.fake.retire_release.set()
        self.fake.release.set()
        self.fleet.close()

    def join_workers(self):
        # Qualification bound only: never increase production timeouts.
        for worker in list(self.fleet._threads):
            worker.join(3)
            self.assertFalse(worker.is_alive())

    def test_cap_holds_until_stop_and_retirement_complete(self):
        self.fake.retire_release.clear()
        for index in range(4):
            self.fleet.enqueue(task(str(index)))
        self.fake.wait_for(lambda: len(self.fake.ran) == 3)
        self.assertEqual(self.fleet.status()["queued"], ["3"])
        self.fake.release.set()
        self.fake.wait_for(lambda: len(self.fake.retired) == 3)
        self.assertEqual(len(self.fake.created), 3)
        self.assertEqual(len(self.fleet.status()["active"]), 3)
        self.fake.retire_release.set()
        self.fake.wait_for(lambda: len(self.fake.ran) == 4)
        self.join_workers()
        self.assertEqual(self.fake.peak, 3)
        self.assertEqual(len(self.fake.retired), 4)

    def test_each_job_gets_fresh_native_session_and_exact_bindings(self):
        self.fake.release.set()
        for index in range(3):
            self.fleet.enqueue(task(str(index)))
        self.join_workers()
        self.assertEqual(len({sid for sid, _, _ in self.fake.ran}), 3)
        self.assertTrue(all(binding is self.config for binding in self.fake.bindings))
        self.assertEqual({item["state"] for item in self.fleet.status()["results"]}, {"finished"})
        self.assertTrue(all(item["retired"] for item in self.fleet.status()["results"]))

    def test_duplicate_enqueue_never_runs_again_and_changed_content_refuses(self):
        self.fake.release.set()
        self.fleet.enqueue(task("one"))
        self.join_workers()
        self.assertTrue(self.fleet.enqueue(task("one"))["duplicate"])
        changed = dict(task("one"), message="different")
        with self.assertRaises(disposable.AdmissionRefused):
            self.fleet.enqueue(changed)
        self.assertEqual(len(self.fake.created), 1)
        self.assertEqual(len(self.fleet.status()["results"]), 1)

    def test_queued_cancel_creates_no_session(self):
        for identifier in ("a", "b", "c", "queued"):
            self.fleet.enqueue(task(identifier))
        self.fake.wait_for(lambda: len(self.fake.ran) == 3)
        self.fleet.cancel("queued")
        self.fleet.cancel("queued")
        self.assertNotIn("queued", self.fake.created)
        terminal = self.fleet.status()["results"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["state"], "cancelled")
        self.assertIsNone(terminal[0]["sessionId"])

    def test_running_cancel_stops_then_retires_once(self):
        self.fleet.enqueue(task("one"))
        self.fake.wait_for(lambda: len(self.fake.ran) == 1)
        for _ in range(4):
            self.fleet.cancel("one")
        self.join_workers()
        result = self.fleet.status()["results"][0]
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["retired"])
        self.assertEqual(self.fake.retired, ["ses_1"])
        self.assertEqual(self.fake.running, 0)

    def test_cancel_during_create_sends_no_prompt_and_retires(self):
        self.fake.create_release.clear()
        self.fleet.enqueue(task("one"))
        self.fake.wait_for(lambda: len(self.fake.created) == 1)
        self.fleet.cancel("one")
        self.fake.create_release.set()
        self.join_workers()
        self.assertEqual(self.fake.ran, [])
        self.assertEqual(self.fake.retired, ["ses_1"])
        self.assertEqual(self.fleet.status()["results"][0]["state"], "cancelled")

    def test_failed_job_still_retires_and_allows_next(self):
        self.fake.state = "failed"
        self.fake.release.set()
        self.fleet.enqueue(task("one"))
        self.join_workers()
        self.fleet.enqueue(task("two"))
        self.join_workers()
        self.assertEqual(self.fake.retired, ["ses_1", "ses_2"])
        self.assertIsNone(self.fleet.status()["blocked"])
        self.assertEqual({item["state"] for item in self.fleet.status()["results"]}, {"failed"})

    def test_retirement_failure_surfaces_and_blocks_further_admission(self):
        self.fake.retire_error = True
        self.fake.release.set()
        self.fleet.enqueue(task("one"))
        self.join_workers()
        status = self.fleet.status()
        self.assertIn("retirement failed", status["blocked"])
        self.assertEqual(status["active"], ["one"])
        self.assertFalse(status["results"][0]["retired"])
        with self.assertRaises(disposable.AdmissionRefused):
            self.fleet.enqueue(task("two"))
        self.fleet.cancel("one")
        self.assertEqual(self.fake.retired, ["ses_1"])

    def test_ambiguous_creation_is_never_retried(self):
        self.fake.create_error = True
        self.fleet.enqueue(task("one"))
        self.join_workers()
        self.assertIn("Ambiguous create", self.fleet.status()["blocked"])
        self.assertTrue(self.fleet.enqueue(task("one"))["duplicate"])
        self.assertEqual(self.fake.created, ["one"])
        self.assertEqual(self.fake.ran, [])
        self.assertEqual(self.fake.retired, [])

    def test_unverified_native_stop_never_deletes_or_reuses_slot(self):
        self.fake.stopped = False
        self.fake.release.set()
        self.fleet.enqueue(task("one"))
        self.join_workers()
        self.assertEqual(self.fake.retired, [])
        self.assertEqual(self.fleet.status()["active"], ["one"])
        self.assertIsNotNone(self.fleet.status()["blocked"])

    def test_run_exception_does_not_assume_process_stopped(self):
        self.fake.run_error = True
        self.fake.release.set()
        self.fleet.enqueue(task("one"))
        self.join_workers()
        self.assertEqual(self.fake.retired, [])
        self.assertIn("Native stop unknown", self.fleet.status()["blocked"])

    def test_reused_native_identity_never_receives_second_prompt_or_delete(self):
        self.fake.reuse = True
        self.fake.release.set()
        self.fleet.enqueue(task("one"))
        self.join_workers()
        self.fleet.enqueue(task("two"))
        self.join_workers()
        self.assertEqual(len(self.fake.ran), 1)
        self.assertEqual(self.fake.retired, ["ses_1"])
        self.assertIn("reused session", self.fleet.status()["blocked"])

    def test_old_session_resume_and_recovery_fields_refused(self):
        for key in ("sessionId", "previousTaskId", "recoveryId", "lane", "resume", "history"):
            with self.subTest(key=key), self.assertRaises(disposable.AdmissionRefused):
                self.fleet.enqueue(dict(task("old"), **{key: "old"}))
        self.assertEqual(self.fake.created, [])

    def test_missing_proof_accepts_no_job(self):
        self.fake.allowed = False
        with self.assertRaises(disposable.AdmissionRefused):
            self.fleet.enqueue(task("one"))
        self.assertEqual(self.fleet.status()["queued"], [])
        self.assertEqual(self.fake.created, [])

    def test_proof_expiring_after_create_sends_no_prompt_but_still_retires(self):
        self.fake.expire_after_create = True
        self.fleet.enqueue(task("one"))
        self.join_workers()
        self.assertEqual(self.fake.ran, [])
        self.assertEqual(self.fake.retired, ["ses_1"])
        status = self.fleet.status()
        self.assertEqual(status["active"], [])
        self.assertIn("eligibility", status["blocked"])
        self.assertEqual(status["results"][0]["state"], "failed")
        self.assertTrue(status["results"][0]["retired"])

    def test_waiting_queue_is_bounded_independently_of_active_slots(self):
        for index in range(3 + disposable.MAX_QUEUED):
            self.fleet.enqueue(task(str(index)))
        self.fake.wait_for(lambda: len(self.fake.ran) == 3)
        with self.assertRaises(disposable.AdmissionRefused):
            self.fleet.enqueue(task("overflow"))
        self.assertEqual(len(self.fleet.status()["queued"]), disposable.MAX_QUEUED)
        self.assertEqual(len(self.fake.created), 3)

    def test_concurrent_enqueues_and_duplicate_cancel_have_one_owner(self):
        threads = [threading.Thread(target=self.fleet.enqueue, args=(task("same"),))
                   for _ in range(12)]
        for worker in threads:
            worker.start()
        for worker in threads:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.fake.wait_for(lambda: len(self.fake.ran) == 1)
        threads = [threading.Thread(target=self.fleet.cancel, args=("same",)) for _ in range(12)]
        for worker in threads:
            worker.start()
        for worker in threads:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.join_workers()
        self.assertEqual(self.fake.created, ["same"])
        self.assertEqual(self.fake.retired, ["ses_1"])
        self.assertEqual(len(self.fleet.status()["results"]), 1)

    def test_direct_config_construction_cannot_raise_cap(self):
        forged = disposable.Config(self.config.executable, self.config.account,
                                   self.config.cwd, disposable.MODEL, 10)
        with self.assertRaises(disposable.AdmissionRefused):
            disposable.Fleet(forged, self.fake)

    def test_config_rejects_guessed_or_excess_capacity_and_models(self):
        for cap in (0, 4, 10, True, "3", None):
            with self.subTest(cap=cap), self.assertRaises(disposable.AdmissionRefused):
                disposable.Config.parse(dict(self.raw, maxConcurrent=cap))
        for model in ("opencode/paid", "opencode/minimax-m3.1-flash-free", None):
            with self.subTest(model=model), self.assertRaises(disposable.AdmissionRefused):
                disposable.Config.parse(dict(self.raw, model=model))
        for key in self.raw:
            missing = dict(self.raw)
            del missing[key]
            with self.subTest(key=key), self.assertRaises(disposable.AdmissionRefused):
                disposable.Config.parse(missing)

    def test_account_is_an_opaque_nonsecret_binding_never_a_file_contract(self):
        for binding in ("existing-opencode-account", "provider:account-id", "account@example.test"):
            with self.subTest(binding=binding), patch.object(
                    Path, "resolve", autospec=True, side_effect=Path.resolve) as resolve:
                parsed = disposable.Config.parse(dict(self.raw, account=binding))
                self.assertEqual(parsed.account, binding)
                self.assertEqual([str(call.args[0]) for call in resolve.call_args_list],
                                 [self.raw["executable"], self.raw["cwd"]])
        for binding in ("", " secret ", "a\nb", "a\tb", "a\x00b", "a\x7fb", "a\u200bb", "x" * 257):
            with self.subTest(binding=repr(binding)), self.assertRaises(disposable.AdmissionRefused):
                disposable.Config.parse(dict(self.raw, account=binding))

    def test_close_cancels_owned_jobs_and_never_imports_old_queue(self):
        for identifier in ("a", "b", "c", "queued"):
            self.fleet.enqueue(task(identifier))
        self.fake.wait_for(lambda: len(self.fake.ran) == 3)
        self.fleet.close()
        self.assertEqual(len(self.fake.created), 3)
        self.assertEqual(len(self.fake.retired), 3)
        self.assertEqual(len(self.fleet.status()["results"]), 4)
        self.assertEqual({item["state"] for item in self.fleet.status()["results"]}, {"cancelled"})
        with self.assertRaises(disposable.AdmissionRefused):
            self.fleet.enqueue(task("new"))

    def test_supported_entrypoint_inspection_has_no_native_or_legacy_effect(self):
        config_path = Path(self.tmp.name) / "dispatch.json"
        config_path.write_text(json.dumps(self.raw))
        before = set(Path(self.tmp.name).iterdir())
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "bin/fleet"),
                                 "disposable-opencode", "--config", str(config_path), "--inspect"],
                                capture_output=True, text=True, check=True,
                                env=dict(os.environ, FLEET_ROOT=str(Path(self.tmp.name) / "no-root"),
                                         FLEET_CONFIG=str(Path(self.tmp.name) / "no-legacy.json")))
        report = json.loads(result.stdout)
        self.assertFalse(report["ready"])
        self.assertIn("Configured OpenCode executable is missing or changed", report["reason"])
        self.assertEqual(set(Path(self.tmp.name).iterdir()), before)

    def test_supported_entrypoint_refusal_is_json_nonzero_and_has_no_effect(self):
        config_path = Path(self.tmp.name) / "dispatch.json"
        config_path.write_text(json.dumps(self.raw))
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "bin/fleet"),
                                 "disposable-opencode", "--config", str(config_path)],
                                input=json.dumps({"enqueue": task("one")}) + "\n",
                                capture_output=True, text=True,
                                env=dict(os.environ, FLEET_ROOT=str(Path(self.tmp.name) / "no-root")))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Configured OpenCode executable is missing or changed",
                      json.loads(result.stdout)["refused"])
        self.assertEqual(result.stderr, "")
        self.assertFalse((Path(self.tmp.name) / "no-root").exists())

    def test_command_refuses_before_job_input_and_never_uses_shell_fallback(self):
        config_path = Path(self.tmp.name) / "dispatch.json"
        config_path.write_text(json.dumps(self.raw))
        with patch("subprocess.Popen", side_effect=AssertionError("native call")):
            with self.assertRaises(disposable.AdmissionRefused):
                disposable.command(config_path, io.StringIO(json.dumps({"enqueue": task("a")})),
                                   lambda value: None)

    def command_jobs(self, count, interrupt=False, interrupt_drain=False):
        config_path = Path(self.tmp.name) / "dispatch.json"
        config_path.write_text(json.dumps(self.raw))
        outputs = []

        def stream():
            for index in range(count):
                yield json.dumps({"enqueue": task(str(index))}) + "\n"
            self.fake.wait_for(lambda: len(self.fake.ran) == min(count, 3))
            if interrupt:
                raise KeyboardInterrupt
            if not interrupt_drain:
                self.fake.release.set()

        drain = (patch.object(disposable.Fleet, "drain", side_effect=KeyboardInterrupt)
                 if interrupt_drain else nullcontext())
        lock_file = Path(self.tmp.name) / "manager.lock"
        lock_file.touch()
        lock_fd = os.open(lock_file, os.O_RDWR)
        with patch.object(disposable, "NativeExecutor", return_value=self.fake), \
                patch.object(disposable, "_acquire_manager_lock", return_value=lock_fd), drain:
            if interrupt or interrupt_drain:
                with self.assertRaises(KeyboardInterrupt):
                    disposable.command(config_path, stream(), outputs.append)
            else:
                disposable.command(config_path, stream(), outputs.append)
        return outputs, [item["terminal"] for item in outputs if "terminal" in item]

    def test_command_eof_drains_more_than_cap_jobs_to_terminal_and_retirement(self):
        outputs, terminals = self.command_jobs(7)
        self.assertEqual(len(terminals), 7)
        self.assertEqual({item["state"] for item in terminals}, {"finished"})
        self.assertTrue(all(item["retired"] for item in terminals))
        self.assertEqual(len(self.fake.created), 7)
        self.assertEqual(len(set(self.fake.retired)), 7)
        self.assertEqual(self.fake.peak, 3)
        self.assertTrue(outputs[-1]["closed"])
        self.assertEqual(outputs[-1]["active"], [])

    def test_command_eof_retirement_failure_terminalizes_waiting_jobs_without_hang(self):
        self.fake.retire_error = True
        outputs, terminals = self.command_jobs(7)
        self.assertEqual(len(terminals), 7)
        self.assertEqual({item["state"] for item in terminals}, {"failed"})
        self.assertEqual(len(self.fake.created), 3)
        self.assertEqual(len(self.fake.retired), 3)
        self.assertEqual(sum(item["sessionId"] is None for item in terminals), 4)
        self.assertIn("retirement failed", outputs[-1]["blocked"])
        self.assertTrue(outputs[-1]["closed"])

    def test_command_interrupt_cancels_instead_of_draining(self):
        outputs, terminals = self.command_jobs(7, interrupt=True)
        self.assertEqual(len(terminals), 7)
        self.assertEqual({item["state"] for item in terminals}, {"cancelled"})
        self.assertEqual(len(self.fake.created), 3)
        self.assertEqual(len(self.fake.retired), 3)
        self.assertTrue(outputs[-1]["closed"])

    def test_command_interrupt_after_eof_still_cancels_and_retires(self):
        outputs, terminals = self.command_jobs(7, interrupt_drain=True)
        self.assertEqual(len(terminals), 7)
        self.assertEqual({item["state"] for item in terminals}, {"cancelled"})
        self.assertEqual(len(self.fake.created), 3)
        self.assertEqual(len(self.fake.retired), 3)
        self.assertTrue(outputs[-1]["closed"])


class NativeExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        executable = root / "opencode"
        executable.write_text("stub")
        executable.chmod(0o755)
        self.config = disposable.Config.parse({
            "executable": str(executable), "account": "opencode",
            "cwd": str(root), "model": disposable.MODEL, "maxConcurrent": 3,
        })
        self.executor = disposable.NativeExecutor()

    def test_verify_requires_exact_model_and_existing_provider(self):
        provider_response = {"data": [{"id": "opencode", "integrationID": "opencode"}]}
        with patch("fleet.disposable.subprocess.run", side_effect=[
                SimpleNamespace(returncode=0, stdout=disposable.MODEL + "\n", stderr=""),
                SimpleNamespace(returncode=0, stdout=json.dumps(provider_response), stderr=""),
        ]) as run:
            self.assertTrue(self.executor.verify(self.config))
        self.assertEqual(run.call_args_list[0].args[0], [self.config.executable, "models"])
        self.assertEqual(run.call_args_list[1].args[0][-2:], ["GET", "/api/provider"])

    def test_verify_refuses_when_exact_model_is_not_listed(self):
        with patch("fleet.disposable.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="opencode/other-model\n", stderr="")):
            with self.assertRaisesRegex(disposable.AdmissionRefused, "Exact configured"):
                self.executor.verify(self.config)

    def test_create_sets_exact_model_and_worktree_then_reads_identity_back(self):
        worktree = str(Path(self.tmp.name).resolve() / "job")
        session_id = "ses_native123"
        responses = [
            {"data": {"id": session_id}},
            {"data": {"id": session_id, "directory": worktree,
                       "model": {"providerID": "opencode", "id": "mimo-v2.6-flash-free"}}},
        ]
        with patch.object(self.executor, "job_cwd", return_value=worktree), \
                patch.object(self.executor, "_call", side_effect=responses) as call:
            self.assertEqual(self.executor.create(
                self.config, disposable.Job("job", "do work", worktree)), session_id)
        self.assertEqual(call.call_args_list[0].args[1:3], ("POST", "/api/session"))
        self.assertEqual(call.call_args_list[0].args[3], {
            "title": "Fleet-disposable:job", "location": {"directory": worktree},
            "model": {"providerID": "opencode", "id": "mimo-v2.6-flash-free"},
        })
        self.assertEqual(call.call_args_list[1].args[1:3],
                         ("GET", "/api/session/" + session_id))

    def test_retire_refuses_while_native_session_is_active(self):
        session_id = "ses_native123"
        with patch.object(self.executor, "_session_tree", return_value=[session_id]), \
                patch.object(self.executor, "_active_ids", return_value={session_id}), \
                patch.object(self.executor, "_call") as call:
            with self.assertRaisesRegex(disposable.AdmissionRefused, "active OpenCode session"):
                self.executor.retire(self.config, session_id)
        call.assert_not_called()

    def test_manager_lock_is_singleton_under_durable_fleet_state(self):
        with patch("fleet.disposable.Path.home", return_value=Path(self.tmp.name)):
            lock_fd = disposable._acquire_manager_lock(self.config)
            self.addCleanup(os.close, lock_fd)
            with self.assertRaisesRegex(disposable.AdmissionRefused, "already owns"):
                disposable._acquire_manager_lock(self.config)
        self.assertTrue((Path(self.tmp.name) / "Developer/.codex/fleet").is_dir())


if __name__ == "__main__":
    unittest.main()
