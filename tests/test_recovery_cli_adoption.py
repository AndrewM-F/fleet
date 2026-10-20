"""The documented adoption CLI must pass its recorded owner to recovery."""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet import cli  # noqa: E402


class RecoveryCliAdoptionTests(unittest.TestCase):
    def args(self):
        return SimpleNamespace(
            worker="ses_w7", task="failed", turn="turn", recovery_id="recovery",
            successor_task="successor", ownership_task="",
            adoption_id="adoption", direct_turn_receipt="",
            owned_worktree="/worktree", branch="repair", head="a" * 40,
            worktree_sha256="b" * 64, note="verified local startup failure")

    def test_adoption_routes_recorded_owner_task(self):
        db = object()
        with patch.object(cli, "database", return_value=db), \
                patch.object(cli.recovery, "adoption_record",
                             return_value={"owner_task_id": "historical-owner"}) as read, \
                patch.object(cli.recovery, "authorize_opencode",
                             return_value={"status": "authorized"}) as authorize, \
                patch.object(cli, "out"):
            cli.cmd_recover_opencode(self.args())
        read.assert_called_once_with(db, "adoption")
        self.assertEqual(authorize.call_args.kwargs["ownership_task_id"],
                         "historical-owner")

    def test_missing_adoption_fails_without_authorization(self):
        with patch.object(cli, "database", return_value=object()), \
                patch.object(cli.recovery, "adoption_record", return_value=None), \
                patch.object(cli.recovery, "authorize_opencode") as authorize, \
                patch.object(cli, "out"):
            with self.assertRaisesRegex(ValueError, "Recovery adoption record is missing"):
                cli.cmd_recover_opencode(self.args())
        authorize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
