"""Offline operator-review controls: no real native service or prompt is used."""
from contextlib import ExitStack
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fleet import disposable as d
from fleet import terminal_review as t


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.run = self.root / "receipts"
        self.run.mkdir()
        self.cwd = self.root / "worktree"
        self.cwd.mkdir()
        self.quota = self.root / "quota.json"
        self.quota.write_text(json.dumps({"version": 1, "holds": {
            d.MODEL: {"verified": True, "evidence": "Fixture verified free exhaustion"}},
            "signatures": []}))
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({"executable": str(self.root / "opencode"),
            "account": "opencode", "cwd": str(self.root), "model": d.MODEL,
            "maxConcurrent": 3, "fallbackModel": d.PAID_MODEL,
            "quotaEvidencePath": str(self.quota)}))
        self.config = d.Config.parse(json.loads(self.config_path.read_text()))
        self.pin = {**t.PIN, "cwd": str(self.cwd), "messageCount": 3,
                    "configSHA256": self.sha(self.config_path),
                    "quotaSHA256": self.sha(self.quota),
                    "runtimeSHA256": self.sha(Path(d.__file__)), "fileHashes": {}}
        self.messages = [
            {"id": "msg_prompt", "type": "user", "content": [{"type": "text", "text": "fixture"}]},
            {"id": self.pin["latestAssistantID"], "type": "assistant", "finish": "stop",
             "content": [{"type": "reasoning", "text": "fixture"}]},
            {"id": self.pin["latestMessageID"], "type": "idle", "outcome": "succeeded"},
        ]
        self.pin["messagesSHA256"] = t.digest(self.messages)
        self.originals = {}
        for name in t.PIN["fileHashes"]:
            value = {"historical": name}
            if name == "superseding-status-20261025.json":
                value = {"completedBoundedScope": {"pullRequest": 1637, "state": "MERGED",
                    "publishedHead": self.pin["head"], "mergeCommit": self.pin["merge"],
                    "rootReviewed": True}}
            path = self.run / name
            path.write_text(json.dumps(value))
            self.originals[name] = path.read_bytes()
            self.pin["fileHashes"][name] = self.sha(path)
        self.session = {"id": self.pin["session"], "projectID": self.pin["project"],
            "title": "Fleet-disposable:" + self.pin["job"],
            "location": {"directory": str(self.cwd)},
            "model": {"providerID": "opencode-go", "id": "mimo-v2.6-flash", "variant": "default"},
            "cost": 1.25, "tokens": {"input": 100, "output": 10},
            "time": {"created": 1, "updated": 2, "idle": 3}}
        self.pin["sessionSHA256"] = t.digest(self.session)
        self.inbox = {"data": []}
        self.permissions = {"data": []}
        self.active = {}
        self.children = []
        self.extra = []
        self.history_response = None
        self.pages = False
        self.missing_session = False
        self.session_inventory = None
        self.calls = []
        self.git_values = iter([self.pin["head"], self.pin["branch"], ""])
        self.executor = d.NativeExecutor()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("RUN", self.run), ("CONFIG", self.config_path), ("PIN", self.pin)):
            self.stack.enter_context(patch.object(t, name, value))
        self.stack.enter_context(patch.object(self.executor, "verify", side_effect=self.verify))
        self.stack.enter_context(patch.object(self.executor, "_call", side_effect=self.api))
        self.stack.enter_context(patch.object(t.subprocess, "check_output", side_effect=self.git))
        self.lock = self.stack.enter_context(patch.object(d, "_acquire_manager_lock", side_effect=self.acquire))

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def acquire(self, config):
        fd = os.open(self.root / "account.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd

    def verify(self, config):
        state = self.executor._quota(config)
        if "paidFailure" in state or d.PAID_MODEL in state["holds"]:
            raise d.AdmissionRefused("Paid hold remains")

    def git(self, args, **kwargs):
        return next(self.git_values)

    def api(self, config, method, path, *args, **kwargs):
        self.calls.append((method, path))
        if method != "GET":
            raise AssertionError("Operator review must never mutate a native session")
        base = self.executor._session_path(self.pin["session"])
        if path == base:
            if self.missing_session:
                raise d.OpenCodeApiError("HTTP 404", status=404)
            return {"data": copy.deepcopy(self.session)}
        if path == base + "/inbox":
            return copy.deepcopy(self.inbox)
        if path == base + "/permission":
            return copy.deepcopy(self.permissions)
        if path.startswith(base + "/message?"):
            if self.pages:
                return ({"data": copy.deepcopy(self.messages[:1]), "cursor": {"next": "next"}}
                        if "order=asc" in path else {"data": copy.deepcopy(self.messages[1:])})
            return copy.deepcopy(self.history_response or {"data": self.messages})
        if path == "/api/session/active":
            return {"data": copy.deepcopy(self.active)}
        if path.startswith("/api/session?"):
            if "parentID=" in path:
                return {"data": copy.deepcopy(self.children)}
            if self.session_inventory is not None:
                if isinstance(self.session_inventory, list):
                    page = 0 if "cursor=" not in path else 1
                    return copy.deepcopy(self.session_inventory[page])
                return copy.deepcopy(self.session_inventory)
            items = [copy.deepcopy(self.session), *copy.deepcopy(self.extra)]
            if "search=Fleet-disposable" in path:
                items = [item for item in items if isinstance(item, dict)
                         and "Fleet-disposable" in item.get("title", "")]
            return {"data": items}
        raise AssertionError(path)

    def review(self, apply=True):
        self.git_values = iter([self.pin["head"], self.pin["branch"], ""])
        return t.run_review(apply=apply, executor=self.executor)

    def assert_refused_without_receipt(self):
        with self.assertRaises((d.AdmissionRefused, OSError)):
            self.review()
        self.assertFalse((self.run / t.RECEIPT).exists())
        self.assertTrue(all(method == "GET" for method, _ in self.calls))

    def test_inspection_has_no_effects_and_same_account_lock(self):
        before = self.quota.read_bytes()
        result = self.review(apply=False)
        self.assertIn("no effects", result["state"])
        self.assertFalse((self.run / t.RECEIPT).exists())
        self.assertEqual(before, self.quota.read_bytes())
        self.lock.assert_called_once_with(self.config)

    def test_review_is_append_only_and_revalidates_normal_clean_start(self):
        with self.assertRaises(d.AdmissionRefused):
            self.executor.verify_clean_start(self.config)
        before = self.quota.read_bytes()
        result = self.review()
        self.assertIn("native-session-preserved", result["state"])
        self.assertTrue(self.executor.verify_clean_start(self.config))
        first = (self.run / t.RECEIPT).read_bytes()
        self.assertIn("already-reviewed", self.review()["state"])
        self.assertEqual(first, (self.run / t.RECEIPT).read_bytes())
        self.assertEqual(before, self.quota.read_bytes())
        for name, body in self.originals.items():
            self.assertEqual(body, (self.run / name).read_bytes())
        self.assertTrue(all(method == "GET" for method, _ in self.calls))
        self.assertEqual(0o600, (self.run / t.RECEIPT).stat().st_mode & 0o777)

    def test_original_job_id_is_refused_before_dispatch(self):
        self.review()
        fleet = d.Fleet(self.config, self.executor)
        with self.assertRaisesRegex(d.AdmissionRefused, "never replay"):
            fleet.enqueue({"id": self.pin["job"], "message": "must not run", "rootAuthorized": True})
        self.assertEqual([], fleet.status()["active"])
        self.assertTrue(all(method == "GET" for method, _ in self.calls))

    def test_renamed_or_missing_exact_session_cannot_hide_behind_search(self):
        self.review()
        self.session["title"] = "renamed"
        with self.assertRaises(d.AdmissionRefused):
            self.executor.verify_clean_start(self.config)
        self.session["title"] = "Fleet-disposable:" + self.pin["job"]
        self.missing_session = True
        with self.assertRaises(d.AdmissionRefused):
            self.executor.verify_clean_start(self.config)

    def test_renamed_unreviewed_session_still_requires_operator_review(self):
        self.session["title"] = "renamed"
        with self.assertRaises(d.AdmissionRefused):
            self.executor.verify_clean_start(self.config)

    def test_incomplete_or_malformed_inventory_cannot_hide_other_leftovers(self):
        self.review()
        for response in ({"data": [self.session], "cursor": {"next": ""}},
                         {"data": [self.session], "cursor": None},
                         {"data": [self.session, None]},
                         {"data": [self.session, {}]},
                         {"data": [self.session, {"id": "ses_bad", "title": 42}]}):
            with self.subTest(response=response):
                self.session_inventory = response
                with self.assertRaises(d.AdmissionRefused):
                    self.executor.verify_clean_start(self.config)
        self.session_inventory = [
            {"data": [self.session], "cursor": {"next": "next"}},
            {"data": [{"id": "ses_other", "title": "Fleet-disposable:other"}]},
        ]
        with self.assertRaises(d.AdmissionRefused):
            self.executor.verify_clean_start(self.config)

    def test_unknown_leftover_still_blocks_after_review(self):
        self.review()
        self.extra = [{"id": "ses_other", "title": "Fleet-disposable:other"}]
        with self.assertRaises(d.AdmissionRefused):
            self.executor.verify_clean_start(self.config)

    def test_complete_history_is_verified_across_pages(self):
        self.pages = True
        self.review()
        self.assertTrue(self.executor.verify_clean_start(self.config))
        self.assertTrue(any("cursor=next" in path for _, path in self.calls))

    def test_boolean_version_cannot_impersonate_integer_receipt(self):
        self.review()
        path = self.run / t.RECEIPT
        value = json.loads(path.read_text())
        value["version"] = True
        path.write_text(json.dumps(value))
        with self.assertRaises(d.AdmissionRefused):
            self.executor.verify_clean_start(self.config)

    def test_completed_original_job_is_refused_even_before_review(self):
        fleet = d.Fleet(self.config, self.executor)
        with self.assertRaisesRegex(d.AdmissionRefused, "never replay"):
            fleet.enqueue({"id": self.pin["job"], "message": "must not run", "rootAuthorized": True})
        self.assertEqual([], self.calls)

    def test_new_work_after_review_invalidates_clean_start(self):
        self.review()
        self.messages.append({"id": "msg_new", "type": "user"})
        with self.assertRaises(d.AdmissionRefused):
            self.executor.verify_clean_start(self.config)

    def test_pending_permission_and_malformed_reads_fail_closed(self):
        for value in ({"data": None}, {"data": {}}, {"data": [{"id": "pending"}]}, {},
                      {"data": [], "unexpected": True}):
            with self.subTest(value=value):
                self.permissions = value
                self.assert_refused_without_receipt()

    def test_pending_inbox_fails_closed(self):
        self.inbox = {"data": [{"id": "queued"}]}
        self.assert_refused_without_receipt()

    def test_active_owner_and_children_fail_closed(self):
        self.active = {self.pin["session"]: {}}
        self.assert_refused_without_receipt()
        self.active = {"ses_other": {}}
        self.assert_refused_without_receipt()
        self.active = {}
        self.children = [{"id": "ses_child"}]
        self.assert_refused_without_receipt()

    def test_identity_variants_and_extra_keys_fail_closed(self):
        original = copy.deepcopy(self.session)
        mutations = [("id", "ses_other"), ("projectID", "foreign"), ("title", "other"),
                     ("cost", 999), ("tokens", {"input": 999}),
                     ("location", {"directory": "/tmp/foreign"}),
                     ("model", {"id": "mimo-v2.6-flash", "providerID": "opencode-go", "variant": "other"}),
                     ("model", {**original["model"], "unexpected": True})]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                self.session = {**copy.deepcopy(original), field: value}
                self.assert_refused_without_receipt()
        self.session = original

    def test_terminal_history_and_pagination_fail_closed(self):
        for response in ({"data": None}, {"data": ["not a message"]},
                         {"data": self.messages, "cursor": None},
                         {"data": self.messages, "cursor": {"next": "loop"}}):
            with self.subTest(response=response):
                self.history_response = response
                self.assert_refused_without_receipt()

    def test_tampered_or_partial_receipt_never_reopens_admission(self):
        self.review()
        path = self.run / t.RECEIPT
        for body in ('{', '{}', '{"version":1,"retryAllowed":true}'):
            path.write_text(body)
            with self.assertRaises(d.AdmissionRefused):
                self.executor.verify_clean_start(self.config)
            with self.assertRaises(d.AdmissionRefused):
                self.review()
            self.assertEqual(body, path.read_text())

    def test_original_latch_and_quota_bytes_cannot_change(self):
        path = self.run / next(iter(self.originals))
        path.write_text("changed")
        self.assert_refused_without_receipt()
        path.write_bytes(self.originals[path.name])
        self.quota.write_text('{"version":1,"holds":{},"signatures":[]}')
        self.assert_refused_without_receipt()

    def test_source_changes_or_competing_account_owner_record_nothing(self):
        with patch.object(t.subprocess, "check_output", return_value="different"):
            self.assert_refused_without_receipt()
        self.lock.side_effect = d.AdmissionRefused("competing owner")
        self.assert_refused_without_receipt()

    def test_receipt_symlink_is_not_followed(self):
        target = self.root / "unrelated.json"
        target.write_text('{}')
        (self.run / t.RECEIPT).symlink_to(target)
        with self.assertRaises(d.AdmissionRefused):
            self.review()
        self.assertEqual('{}', target.read_text())


class SessionInventoryTests(unittest.TestCase):
    def test_cursor_and_session_identity_validation_is_bounded(self):
        executor = d.NativeExecutor()
        for cursor in ("", False, 1, [], "x" * 4097):
            with self.subTest(cursor=cursor), patch.object(executor, "_call", return_value={
                    "data": [{"id": "ses_one"}], "cursor": {"next": cursor}}):
                with self.assertRaises(d.AdmissionRefused):
                    executor._list_sessions(None)
        with patch.object(executor, "_MAX_SESSION_PAGES", 2), patch.object(executor, "_call",
                side_effect=[{"data": [{"id": "ses_one"}], "cursor": {"next": "a"}},
                             {"data": [{"id": "ses_two"}], "cursor": {"next": "b"}}]):
            with self.assertRaisesRegex(d.AdmissionRefused, "page bound"):
                executor._list_sessions(None)
        with patch.object(executor, "_call", side_effect=[
                {"data": [{"id": "ses_one"}], "cursor": {"next": "a"}},
                {"data": [{"id": "ses_one"}]}]):
            with self.assertRaisesRegex(d.AdmissionRefused, "identity repeated"):
                executor._list_sessions(None)



if __name__ == "__main__":
    unittest.main()
