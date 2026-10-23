"""The session receipt lifecycle: add, register, drain, remove.

Synthetic only: a temporary fleet root, a synthetic native session inventory and
the same fake OpenCode CLI the rest of the suite uses. No native session is
created, no manager is activated, and no account, quota or live configuration is
touched.
"""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fleet import opencode_lifecycle, paths  # noqa: E402
from test_lifecycle_findings import FindingSandbox  # noqa: E402

MODEL = "opencode-go/minimax-m3.1-flash-free"


class ReceiptLifecycleTests(FindingSandbox):
    """The receipt follows the lifecycle, and it never moves backwards."""

    def status_of(self, session_id):
        row = self.db.execute("SELECT status FROM opencode_sessions WHERE session_id=?",
                              (session_id,)).fetchone()
        return row[0] if row is not None else None

    def add_one(self, key):
        result = opencode_lifecycle.add_sessions(
            self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key=key)
        self.assertEqual(result["created"], 1)
        identifier = result["sessions"][0]["sessionId"]
        return identifier

    def test_add_registered_drain_remove_moves_the_receipt_forward(self):
        identifier = self.add_one("w5-receipt-001")
        self.assertEqual(self.status_of(identifier), "registered")
        # A drain is a valid transition out of registered.
        drain = opencode_lifecycle.drain_session(self.db, identifier, note="draining")
        self.assertTrue(drain["draining"])
        self.assertEqual(self.status_of(identifier), "draining")
        # And a removal is a valid transition out of draining.
        removal = opencode_lifecycle.remove_session(self.db, identifier, note="removing")
        self.assertTrue(removal["removed"])
        self.assertEqual(self.status_of(identifier), "removed")
        # The roster no longer holds it, and the native session is kept.
        self.assertNotIn(identifier, [entry["sessionId"]
                                      for entry in paths.config()["opencode"]])
        self.assertTrue(removal["nativeSessionKept"])

    def test_a_same_request_key_retry_after_the_transitions_changes_nothing(self):
        identifier = self.add_one("w5-receipt-002")
        opencode_lifecycle.drain_session(self.db, identifier, note="draining")
        opencode_lifecycle.remove_session(self.db, identifier, note="removing")
        # The request is still recorded as registered, so the retry is idempotent
        # and it does not revive the session or move the receipt backwards.
        again = opencode_lifecycle.add_sessions(
            self.db, count=1, model=MODEL, cwd=str(self.wt), root=str(self.root),
            request_key="w5-receipt-002")
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["created"], 0)
        self.assertEqual(self.status_of(identifier), "removed")
        self.assertNotIn(identifier, [entry["sessionId"]
                                      for entry in paths.config()["opencode"]])

    def test_a_stale_created_update_cannot_downgrade_a_registered_receipt(self):
        identifier = self.add_one("w5-receipt-003")
        request = {"model": MODEL, "account": str(self.native), "cwd": str(self.wt),
                   "root": str(self.root), "count": 1, "dispatchEnabled": False}
        opencode_lifecycle._record_session(self.db, identifier, "w5-receipt-003", request,
                                          "created", "a stale reconciliation")
        self.assertEqual(self.status_of(identifier), "registered")

    def test_an_unverified_update_cannot_downgrade_a_registered_receipt(self):
        identifier = self.add_one("w5-receipt-004")
        request = {"model": MODEL, "account": str(self.native), "cwd": str(self.wt),
                   "root": str(self.root), "count": 1, "dispatchEnabled": False}
        opencode_lifecycle._record_session(self.db, identifier, "w5-receipt-004", request,
                                          "unverified", "a reconciliation that could not read it")
        self.assertEqual(self.status_of(identifier), "registered")

    def test_a_stale_update_cannot_downgrade_a_removed_receipt(self):
        identifier = self.add_one("w5-receipt-005")
        opencode_lifecycle.drain_session(self.db, identifier, note="draining")
        opencode_lifecycle.remove_session(self.db, identifier, note="removing")
        request = {"model": MODEL, "account": str(self.native), "cwd": str(self.wt),
                   "root": str(self.root), "count": 1, "dispatchEnabled": False}
        opencode_lifecycle._record_session(self.db, identifier, "w5-receipt-005", request,
                                          "created", "a stale reconciliation")
        opencode_lifecycle._record_session(db=self.db, session_id=identifier,
                                          request_key="w5-receipt-005", request=request,
                                          status="registered", detail="a stale registration")
        self.assertEqual(self.status_of(identifier), "removed")


if __name__ == "__main__":
    unittest.main()
