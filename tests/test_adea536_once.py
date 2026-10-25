"""Offline only: the one-use helper is never executed against native OpenCode."""
from contextlib import ExitStack
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from fleet import disposable as d

spec = importlib.util.spec_from_file_location(
    "adea536_once", Path(__file__).resolve().parents[1] / "scripts/continue-adea536-once.py")
once = importlib.util.module_from_spec(spec)
spec.loader.exec_module(once)
read_inbox = once.inbox


class OnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.run = self.root / "receipts"
        self.run.mkdir()
        self.cwd = self.root / "worktree"
        self.cwd.mkdir()
        executable = self.root / "opencode"
        executable.write_text("offline stub")
        executable.chmod(0o755)
        self.quota = self.root / "quota.json"
        self.quota.write_text(json.dumps({"version": 1, "holds": {
            d.MODEL: {"verified": True, "evidence": "Offline fixture verified free hold"}},
            "signatures": []}))
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"executable": str(executable), "account": "opencode",
            "cwd": str(self.cwd), "model": d.MODEL, "maxConcurrent": 3,
            "fallbackModel": d.PAID_MODEL, "quotaEvidencePath": str(self.quota)}))
        self.task = self.run / "task.txt"
        self.task.write_text("One approved test-only task\n")
        self.pin = {**once.PIN, "runtimeSHA": hashlib.sha256(Path(d.__file__).read_bytes()).hexdigest(),
                    "cwd": str(self.cwd), "config": self.config,
                    "configSHA": hashlib.sha256(self.config.read_bytes()).hexdigest(),
                    "taskSHA": hashlib.sha256(self.task.read_bytes()).hexdigest()}
        self.owner = {"jobID": self.pin["job"], "implementationOwner": self.pin["job"],
            "reviewOwner": self.pin["reviewOwner"], "nativeSessionID": self.pin["session"],
            "worktree": str(self.cwd), "branch": self.pin["branch"], "base": self.pin["head"],
            "status": "ADMISSION_STOPPED_BEFORE_PROMPT; SESSION_PRESERVED; NO_RETRY",
            "workerExecuted": False, "nativeTurnID": None}
        self.write_owner()
        self.session = {"id": self.pin["session"], "projectID": self.pin["project"],
            "title": "Fleet-disposable:" + self.pin["job"], "location": {"directory": str(self.cwd)},
            "model": {"id": "mimo-v2.6-flash", "providerID": "opencode-go", "variant": "default"},
            "cost": 0, "tokens": {"input": 0, "output": 0, "reasoning": 0,
                                   "cache": {"read": 0, "write": 0}}}
        self.calls = []
        self.history = [copy.deepcopy(once.EVENT)]
        self.after = [{"id": "msg_done", "type": "assistant", "finish": "stop",
                       "content": [{"type": "text", "text": "Completed approved work"}]}]
        self.permission_reply = {"data": []}
        self.prompt_error = None
        self.accept_reply = {"data": {"id": "msg_productive_inbox"}}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(once, "RUN", self.run))
        self.stack.enter_context(patch.object(once, "PIN", self.pin))
        self.lock_mock = self.stack.enter_context(patch.object(d, "_acquire_manager_lock",
                                                             side_effect=self.acquire))
        self.stack.enter_context(patch.object(once, "git", side_effect=self.git))
        self.inbox_mock = self.stack.enter_context(patch.object(once, "inbox"))
        self.verify_mock = self.stack.enter_context(patch.object(d.NativeExecutor, "verify"))
        self.stack.enter_context(patch.object(d.NativeExecutor, "job_cwd", return_value=str(self.cwd)))
        self.list_mock = self.stack.enter_context(patch.object(d.NativeExecutor, "_list_sessions",
            side_effect=lambda *args, **filters: [] if "parentID" in filters else
            [{"id": self.pin["session"]}]))
        self.tree_mock = self.stack.enter_context(patch.object(d.NativeExecutor, "_session_tree",
            return_value=[self.pin["session"]]))
        self.active_mock = self.stack.enter_context(patch.object(d.NativeExecutor, "_active_ids",
            return_value=set()))
        self.stack.enter_context(patch.object(d.NativeExecutor, "_messages", side_effect=self.messages))
        self.stack.enter_context(patch.object(d.NativeExecutor, "_call", side_effect=self.api))

    def write_owner(self):
        (self.run / "ownership.json").write_text(json.dumps(self.owner))

    def git(self, *args):
        if args == ("rev-parse", "HEAD"):
            return self.pin["head"]
        if args == ("branch", "--show-current"):
            return self.pin["branch"]
        return ""

    def acquire(self, config):
        self.assertEqual(config.account, "opencode-go")
        self.assertEqual(config.model, d.PAID_MODEL)
        self.assertEqual(config.max_concurrent, 3)
        fd = os.open(self.root / "account.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def messages(self, *_):
        return self.after if any(path.endswith("/prompt") for _, path, _ in self.calls) else self.history

    def api(self, config, method, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        if method == "GET":
            if path.endswith("/permission"):
                return self.permission_reply
            return {"data": copy.deepcopy(self.session)}
        if path.endswith("/model"):
            self.session["model"] = {**body["model"], "variant": "default"}
            return None
        if path.endswith("/prompt"):
            # The same account lock remains held across the productive turn.
            with self.assertRaises(BlockingIOError):
                fd = self.acquire(config)
                os.close(fd)
            if self.prompt_error:
                raise self.prompt_error
            return self.accept_reply
        raise AssertionError("Unexpected native mutation: " + path)

    def prompts(self):
        return [c for c in self.calls if c[1].endswith("/prompt")]

    def assert_no_create_delete(self):
        self.assertFalse(any(method == "DELETE" or (method == "POST" and path == "/api/session")
                             for method, path, _ in self.calls))

    def test_exact_empty_session_continues_once_under_account_lock_without_create_delete(self):
        receipt = once.run_once()
        self.assertEqual(receipt["result"]["state"], "finished")
        self.assertTrue(receipt["sessionPreserved"])
        self.assertEqual(len(self.prompts()), 1)
        self.assertEqual(self.prompts()[0][2], {"text": "One approved test-only task"})
        accepted = json.loads((self.run / "continuation-prompt-accepted.json").read_text())
        self.assertEqual(accepted["inboxID"], "msg_productive_inbox")
        self.assertEqual(accepted["implementationOwner"], self.pin["job"])
        self.assertEqual(accepted["sessionID"], self.pin["session"])
        self.assert_no_create_delete()
        prior = list(self.calls)
        with self.assertRaises(FileExistsError):
            once.run_once()
        self.assertEqual(self.calls, prior)
        fd = self.acquire(self.verify_mock.call_args.args[0])
        os.close(fd)  # Lock released after completion; the one-use latch remains.

    def test_uncertain_prompt_preserves_attempt_and_never_replays(self):
        self.prompt_error = d.OpenCodeApiError("Transport outcome unknown")
        with self.assertRaises(d.OpenCodeApiError):
            once.run_once()
        self.assertEqual(len(self.prompts()), 1)
        self.assertTrue((self.run / "continuation-prompt-intent.json").exists())
        self.assertFalse((self.run / "continuation-prompt-accepted.json").exists())
        with self.assertRaises(FileExistsError):
            once.run_once()
        self.assertEqual(len(self.prompts()), 1)
        self.assert_no_create_delete()

    def test_missing_prompt_acceptance_identity_is_uncertain_and_never_replayed(self):
        self.accept_reply = {"data": {}}
        with self.assertRaisesRegex(d.AdmissionRefused, "acceptance is uncertain"):
            once.run_once()
        self.assertEqual(len(self.prompts()), 1)
        self.assertFalse((self.run / "continuation-prompt-accepted.json").exists())
        with self.assertRaises(FileExistsError):
            once.run_once()
        self.assertEqual(len(self.prompts()), 1)
        self.assert_no_create_delete()

    def test_empty_or_control_prompt_acceptance_identity_is_not_execution_evidence(self):
        for identifier in ("", "\n", "x" * 257):
            with self.subTest(identifier=repr(identifier)):
                for name in ("continuation-attempt.json", "continuation-result.json",
                             "continuation-prompt-intent.json"):
                    (self.run / name).unlink(missing_ok=True)
                self.calls.clear()
                self.accept_reply = {"data": {"id": identifier}}
                with self.assertRaisesRegex(d.AdmissionRefused, "acceptance is uncertain"):
                    once.run_once()
                self.assertEqual(len(self.prompts()), 1)
                self.assertFalse((self.run / "continuation-prompt-accepted.json").exists())

    def test_inbox_reader_requires_exact_empty_response(self):
        config = d.Config.parse(json.loads(self.config.read_text()))
        for payload, code in (({"data": []}, 0), ({"data": [{"id": "msg_queued"}]}, 0),
                              ({"data": [], "cursor": {"next": "unread"}}, 0), ({}, 0),
                              ({"data": []}, 1)):
            with self.subTest(payload=payload, code=code):
                def native_read(command, *, stdout, stderr, **kwargs):
                    self.assertEqual(command[2], "session.inbox.list")
                    self.assertEqual(command[-1], "sessionID=" + self.pin["session"])
                    stdout.write(json.dumps(payload).encode())
                    return subprocess.CompletedProcess(command, code)
                with patch.object(once.subprocess, "run", side_effect=native_read):
                    if payload == {"data": []} and code == 0:
                        read_inbox(config)
                    else:
                        with self.assertRaises(d.AdmissionRefused):
                            read_inbox(config)

    def test_paid_failure_keeps_native_context_and_durable_paid_hold(self):
        self.after = [{"id": "msg_error", "type": "assistant", "finish": "error",
                       "error": {"type": "OfflineError", "message": "Paid error"}}]
        receipt = once.run_once()
        self.assertEqual(receipt["result"]["state"], "failed")
        self.assertTrue(receipt["result"]["preserve_session"])
        self.assertIn("paidFailure", json.loads(self.quota.read_text()))
        self.assertEqual(len(self.prompts()), 1)
        self.assert_no_create_delete()

    def test_paid_timeout_keeps_native_context_and_durable_timeout_hold(self):
        self.after = []
        with patch.object(d.NativeExecutor, "_JOB_TIMEOUT", 0), patch.object(
                d.NativeExecutor, "_POLL_SECONDS", 0):
            receipt = once.run_once()
        self.assertTrue(receipt["result"]["preserve_session"])
        self.assertEqual(json.loads(self.quota.read_text())["paidFailure"]["kind"], "timeout")
        self.assert_no_create_delete()

    def test_duplicate_prompt_call_is_blocked_before_second_native_request(self):
        def twice(executor, config, sid, job, cancel):
            for _ in range(2):
                executor._call(config, "POST", executor._session_path(sid, "/prompt"),
                               {"text": job.message})
        with patch.object(d.NativeExecutor, "run", twice):
            with self.assertRaises(FileExistsError):
                once.run_once()
        self.assertEqual(len(self.prompts()), 1)
        self.assert_no_create_delete()

    def test_preflight_rejects_changed_session_work_owner_or_pending_work(self):
        changes = [
            ("session id", lambda: self.session.update(id="ses_other")),
            ("model", lambda: self.session.update(model={"providerID": "other", "id": "other"})),
            ("variant", lambda: self.session["model"].update(variant="high")),
            ("extra model key", lambda: self.session["model"].update(extra=True)),
            ("directory", lambda: self.session.update(location={"directory": "/other"})),
            ("title", lambda: self.session.update(title="Other owner")),
            ("project", lambda: self.session.update(projectID="other")),
            ("tokens", lambda: self.session["tokens"].update(input=1)),
            ("cost", lambda: self.session.update(cost=1)),
            ("user work", lambda: self.history.append({"id": "msg_user", "type": "user"})),
            ("assistant work", lambda: self.history.append({"id": "msg_assistant", "type": "assistant"})),
            ("event changed", lambda: self.history[0].update(id="msg_other")),
            ("active work", lambda: setattr(self.active_mock, "return_value", {"ses_other"})),
            ("child", lambda: setattr(self.tree_mock, "return_value", [self.pin["session"], "ses_child"])),
            ("another manager", lambda: setattr(self.list_mock, "side_effect", lambda *a, **k: [{"id": "ses_other"}])),
            ("malformed child", lambda: setattr(self.list_mock, "side_effect",
                lambda *a, **k: [{}] if "parentID" in k else [{"id": self.pin["session"]}])),
            ("permission", lambda: setattr(self, "permission_reply", {"data": [{"id": "pending"}]})),
            ("inbox", lambda: setattr(self.inbox_mock, "side_effect", d.AdmissionRefused("inbox changed"))),
            ("owner", lambda: (self.owner.update(implementationOwner="other"), self.write_owner())),
        ]
        baseline = copy.deepcopy(self.session)
        for label, change in changes:
            with self.subTest(label=label):
                self.session = copy.deepcopy(baseline)
                self.history = [copy.deepcopy(once.EVENT)]
                self.active_mock.return_value = set()
                self.tree_mock.return_value = [self.pin["session"]]
                self.list_mock.side_effect = lambda *a, **k: [] if "parentID" in k else [{"id": self.pin["session"]}]
                self.permission_reply = {"data": []}
                self.inbox_mock.side_effect = None
                self.owner["implementationOwner"] = self.pin["job"]
                self.write_owner()
                # Each case has a fresh offline-only attempt; never done by runtime.
                for name in ("continuation-attempt.json", "continuation-result.json"):
                    (self.run / name).unlink(missing_ok=True)
                change()
                with self.assertRaises(d.AdmissionRefused):
                    once.run_once()
                self.assertFalse(self.prompts())
                self.assert_no_create_delete()

    def test_permission_requires_exact_empty_proof_before_prompt(self):
        replies = [
            {"data": {"unexpected": "shape"}},
            {"data": None},
            {"data": "unexpected"},
            {"data": False},
            {"data": [{"id": "permission_pending"}]},
            {"data": [], "cursor": {"next": "unread"}},
            {},
            None,
        ]
        for reply in replies:
            with self.subTest(reply=reply):
                for name in ("continuation-attempt.json", "continuation-result.json"):
                    (self.run / name).unlink(missing_ok=True)
                self.calls.clear()
                self.permission_reply = reply
                with self.assertRaisesRegex(d.AdmissionRefused, "permission response"):
                    once.run_once()
                self.assertFalse(self.prompts())
                self.assertFalse((self.run / "continuation-prompt-intent.json").exists())
                self.assertFalse((self.run / "continuation-prompt-accepted.json").exists())
                self.assert_no_create_delete()

    def test_changed_inputs_or_dirty_checkout_refuse_without_prompt(self):
        for field in ("configSHA", "taskSHA", "runtimeSHA"):
            with self.subTest(field=field), patch.dict(self.pin, {field: "0" * 64}):
                with self.assertRaises(d.AdmissionRefused):
                    once.run_once()
        with patch.object(once, "git", return_value="dirty-or-changed"):
            with self.assertRaises(d.AdmissionRefused):
                once.run_once()
        self.assertFalse(self.prompts())

    def test_account_lock_contention_refuses_before_native_or_receipt_changes(self):
        config = d.Config.parse(json.loads(self.config.read_text()))
        fd = self.acquire(d.replace(config, model=d.PAID_MODEL, account="opencode-go"))
        try:
            with self.assertRaises(BlockingIOError):
                once.run_once()
            self.assertFalse((self.run / "continuation-attempt.json").exists())
            self.assertFalse(self.calls)
        finally:
            os.close(fd)

    def test_one_use_latch_symlink_is_never_followed_or_overwritten(self):
        target = self.root / "unrelated.txt"
        target.write_text("preserve")
        (self.run / "continuation-attempt.json").symlink_to(target)
        with self.assertRaises(FileExistsError):
            once.run_once()
        self.assertEqual(target.read_text(), "preserve")
        self.assertFalse(self.calls)
