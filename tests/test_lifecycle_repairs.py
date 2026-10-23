"""Synthetic regressions for custody-reviewed lifecycle repair findings."""

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

from .test_opencode_lifecycle import MODEL, Sandbox
from fleet import cli, opencode_lifecycle, ops, paths, store


class IntentReplayTests(Sandbox):
    def test_target_retry_finishes_receipts_after_roster_write(self):
        original = opencode_lifecycle._record_session

        def interrupted(db, sid, key, request, status, detail=""):
            if status == "registered":
                raise RuntimeError("synthetic crash after roster write")
            return original(db, sid, key, request, status, detail)

        with patch.object(opencode_lifecycle, "_record_session", interrupted):
            with self.assertRaises(RuntimeError):
                self.add(count=None, target_total=3, request_key="crash-target")
        self.db.execute("UPDATE opencode_provisioning SET creator_pid=0")
        self.db.commit()
        result = self.add(count=None, target_total=3, request_key="crash-target")
        self.assertEqual(result["status"], "registered")
        self.assertEqual(result["registered"], 2)
        self.assertEqual(len(self.creations()), 2)
        self.assertEqual(
            self.db.execute(
                "SELECT status FROM opencode_provisioning WHERE request_key='crash-target'"
            ).fetchone()[0],
            "registered",
        )

    def test_derived_target_key_survives_roster_growth(self):
        first = self.add(count=None, target_total=3)
        self.add(count=1, request_key="unrelated-growth")
        again = self.add(count=None, target_total=3)
        self.assertEqual(again["requestKey"], first["requestKey"])
        self.assertEqual(again["requested"], 2)
        self.assertEqual(again["registered"], 2)
        self.assertEqual(len(self.creations()), 3)

    def test_target_retry_after_roster_shrink_keeps_original_intent(self):
        self.add(count=None, target_total=3, request_key="target")
        cfg = paths.config()
        cfg["opencode"] = cfg["opencode"][:1]
        self.write_config(cfg)
        again = self.add(count=None, target_total=3, request_key="target")
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.creations()), 2)
        self.assertEqual(again["sessions"], [])

    def test_reached_target_does_not_bypass_payload_binding(self):
        self.add(count=None, target_total=2, request_key="bound")
        with self.assertRaisesRegex(ValueError, "different request"):
            self.add(
                count=None, target_total=2, request_key="bound", enable_dispatch=True
            )

    def test_retired_receipts_cannot_create_replacements_on_incomplete_request(self):
        self.add(count=1, request_key="retired")
        self.db.execute("UPDATE opencode_provisioning SET status='failed'")
        self.db.execute("UPDATE opencode_sessions SET status='removed'")
        self.db.commit()
        result = self.add(count=1, request_key="retired")
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(self.creations()), 1)

    def test_generic_remove_obeys_uncertain_work_barrier(self):
        self.task("uncertain-task", self.existing, "uncertain")
        before = paths.CONFIG.read_bytes()
        with self.assertRaisesRegex(ValueError, "outstanding"):
            ops.remove(self.existing)
        self.assertEqual(paths.CONFIG.read_bytes(), before)

    def test_generic_remove_obeys_unreadable_queue_barrier(self):
        paths.QUEUE.write_text("{")
        before = paths.CONFIG.read_bytes()
        with self.assertRaises(ValueError):
            ops.remove(self.existing)
        self.assertEqual(paths.CONFIG.read_bytes(), before)


class CliPlanningTests(Sandbox):
    def command(self):
        return [
            sys.executable,
            str(Path(cli.__file__).parents[2] / "bin/fleet"),
            "add-opencode",
            "--count",
            "1",
            "--model",
            MODEL,
            "--cwd",
            str(self.wt),
            "--root",
            str(self.root),
            "--dry-run",
        ]

    def run_cli(self):
        env = {
            **os.environ,
            "FLEET_ROOT": str(self.root),
            "FLEET_CONFIG": str(paths.CONFIG),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        return subprocess.run(self.command(), env=env, capture_output=True, text=True)

    def durable_bytes(self):
        return {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }

    def test_real_cli_plans_legacy_store_without_migrating_it(self):
        self.db.close()
        paths.DB.unlink()
        with sqlite3.connect(paths.DB) as db:
            db.execute(
                "CREATE TABLE recoveries (id TEXT, failed_sid TEXT, failed_turn TEXT, "
                "UNIQUE(failed_sid, failed_turn))"
            )
            db.execute(
                "CREATE TABLE opencode_provisioning (request_key TEXT PRIMARY KEY, "
                "model TEXT, account TEXT, cwd TEXT, root TEXT, requested_count INTEGER, "
                "dispatch_enabled INTEGER, status TEXT, created REAL, updated REAL, detail TEXT)"
            )
        before = self.durable_bytes()
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["dryRun"])
        self.assertEqual(self.durable_bytes(), before)

    def test_real_cli_missing_data_directory_stays_missing(self):
        self.db.close()
        paths.DB.unlink()
        paths.DATA.rmdir()
        before = self.durable_bytes()
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(paths.DATA.exists())
        self.assertEqual(self.durable_bytes(), before)

    def test_real_cli_missing_root_stays_missing(self):
        missing = self.root / "missing-root"
        command = self.command()
        command[command.index("--root") + 1] = str(missing)
        result = subprocess.run(
            command,
            env={
                **os.environ,
                "FLEET_ROOT": str(missing),
                "FLEET_CONFIG": str(missing / "config/fleet.local.json"),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())

    def test_wal_planning_refuses_without_touching_source(self):
        before = self.durable_bytes()
        with self.assertRaisesRegex(ValueError, "without WAL"):
            store.planning_database()
        self.assertEqual(self.durable_bytes(), before)

    def test_failed_and_uncertain_cli_results_exit_nonzero_with_evidence(self):
        for result in (
            {"status": "failed", "failed": "before start", "requestKey": "k"},
            {
                "status": "uncertain",
                "uncertain": True,
                "requestKey": "k",
                "createdNotRegistered": ["ses_EVIDENCE"],
            },
        ):
            output = io.StringIO()
            with (
                self.subTest(result=result),
                patch.object(cli, "database", return_value=self.db),
                patch.object(opencode_lifecycle, "add_sessions", return_value=result),
                contextlib.redirect_stdout(output),
            ):
                with self.assertRaises(SystemExit) as error:
                    cli.main(
                        [
                            "add-opencode",
                            "--count",
                            "1",
                            "--model",
                            MODEL,
                            "--cwd",
                            str(self.wt),
                            "--root",
                            str(self.root),
                        ]
                    )
                self.assertEqual(error.exception.code, 1)
                self.assertEqual(json.loads(output.getvalue()), result)
