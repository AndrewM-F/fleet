"""Source/data separation and audited recovery of a non-executed wrapper launch."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fleet import holds, manager, opencode, paths, recovery
from tests import test_opencode as adapter_tests


class LauncherSourceTests(unittest.TestCase):
    def test_distinct_data_root_cannot_select_its_stale_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = root / 'bin' / 'fleet'
            stale.parent.mkdir()
            stale.write_text('raise SystemExit("STALE_SOURCE_WAS_SELECTED")\n')
            directory = root / 'data' / 'opencode' / 'ses_fixture' / 'turns' / 'turn-1'
            directory.mkdir(parents=True)
            request = directory / 'request.json'
            request.write_text(json.dumps({
                'sessionId': 'ses_fixture', 'cwd': tmp, 'sessionCwd': tmp,
                'model': 'opencode-go/minimax-m3.1-flash-free', 'turn': 'turn-1',
            }))
            source = Path(__file__).resolve().parents[1]
            script = ('from fleet import paths; import subprocess,sys; '
                      'print(paths.ROOT, flush=True); print(paths.BIN, flush=True); '
                      'raise SystemExit(subprocess.run([sys.executable,str(paths.BIN),'
                      '"opencode-turn",sys.argv[1]]).returncode)')
            env = dict(os.environ, FLEET_ROOT=tmp, PYTHONPATH=str(source / 'src'),
                       PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-c', script, str(request)],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout.splitlines(), [tmp, str(source / 'bin' / 'fleet')])
            self.assertNotIn('STALE_SOURCE_WAS_SELECTED', result.stderr)
            # Reviewed MiniMax validation succeeded; the deliberate missing lock
            # rejects execution before any native DB access or provider process.
            self.assertIn('did not inherit a valid session lock', result.stderr)
            self.assertFalse((directory / 'state.json').exists())


class LaunchFailureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = adapter_tests.OpenCodeAdapterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.sid = self.fixture.sid
        self.turn = 'failed-launch'
        self.slot = dict(self.fixture.slot, model='opencode-go/minimax-m3.1-flash-free',
                         dispatchEnabled=False)
        root = Path(self.fixture.tmp.name)
        for name, value in [('PAUSE', root / 'pause'), ('MANAGER_LOCK', root / 'manager.lock')]:
            value.write_text('paused' if name == 'PAUSE' else '')
            p = patch.object(paths, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.directory = opencode.turn_dir(self.sid, self.turn)
        self.directory.mkdir(parents=True)
        self.pointer = opencode.session_dir(self.sid) / 'current.json'
        self.identity = {'sessionId': self.sid, 'taskId': 'failed-task', 'turn': self.turn,
                         'cwd': str(self.fixture.cwd.resolve()), 'model': self.slot['model']}
        self.launch = dict(self.identity, startedAt=time.time() - 20, wrapperPid=99999999)
        self.pointer.write_text(json.dumps(self.launch))
        self.request = self.directory / 'request.json'
        self.request.write_text(json.dumps(dict(self.identity, databasePath=str(self.fixture.native_db.resolve()),
                                                sessionCwd=self.identity['cwd'])))
        self.log = paths.LOGS / 'opencode' / self.sid / self.turn / 'wrapper.log'
        self.log.parent.mkdir(parents=True)
        self.log.write_text('Traceback (most recent call last):\n'
                            '  raise ValueError("OpenCode request model is not the pinned free model")\n'
                            'ValueError: OpenCode request model is not the pinned free model\n')
        self.db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?)',
                        ('failed-task', self.sid, self.identity['cwd'], '{}', 'finished', 1))
        self.fixture.add_task('held-task')
        holds.hold(self.db, 'held-task', 'existing provider/model quota hold')
        for p in [patch.object(paths, 'opencode_slots', return_value=[self.slot]),
                  patch.object(opencode.os, 'kill', side_effect=ProcessLookupError),
                  patch.object(opencode, '_group_exists', return_value=False)]:
            p.start()
            self.addCleanup(p.stop)

    def reconcile(self):
        return recovery.reconcile_launch_failure(self.db, 'failed-task', self.turn,
                                                'root reviewed known stale-source model guard')

    def test_missing_native_state_finishes_with_explicit_failure_not_audit_success(self):
        self.db.execute("UPDATE tasks SET state='sent' WHERE id='failed-task'")
        self.db.commit()
        state = opencode.state(self.slot)
        self.assertEqual(state['state'], 'blocked')
        self.assertTrue(state['launchFailure'])
        manager.observe(self.db, self.sid, state)
        manager.observe(self.db, self.sid, state)
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id='failed-task'").fetchone(),
                         ('finished',))
        events = self.db.execute("SELECT body FROM events WHERE kind='review_ready'").fetchall()
        self.assertEqual(len(events), 1)
        outcome = json.loads(events[0][0])
        self.assertEqual(outcome['state'], 'blocked')
        self.assertEqual(outcome['outcome'], 'launch_failed')
        self.assertIn('state is not available', outcome['error'])
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE kind='task_uncertain'").fetchone()[0], 0)

    def test_known_prelaunch_failure_preserves_history_holds_and_is_idempotent(self):
        before = self.db.execute('SELECT * FROM tasks ORDER BY id').fetchall()
        held = self.db.execute('SELECT * FROM task_holds').fetchall()
        result = self.reconcile()
        self.assertTrue(result['reconciled'])
        self.assertEqual(result['evidence']['nativeMessagesSinceLaunch'], 0)
        self.assertEqual(self.reconcile()['record'], result['record'])
        self.assertEqual(self.db.execute('SELECT * FROM tasks ORDER BY id').fetchall(), before)
        self.assertEqual(self.db.execute('SELECT * FROM task_holds').fetchall(), held)
        self.assertTrue(paths.PAUSE.exists())
        self.assertFalse(self.slot['dispatchEnabled'])
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE kind='opencode_launch_reconciled'").fetchone()[0], 1)
        self.assertEqual(opencode.state(self.slot)['state'], 'stopped')
        self.assertFalse((self.directory / 'state.json').exists())
        self.assertFalse((self.directory / 'result.json').exists())

    def test_uncertain_task_is_not_rewritten_by_launch_reconciliation(self):
        self.db.execute("UPDATE tasks SET state='uncertain' WHERE id='failed-task'")
        self.db.commit()
        self.assertEqual(self.reconcile()['taskStatePreserved'], 'uncertain')

    def test_native_message_metadata_blocks_no_inference_claim(self):
        with sqlite3.connect(self.fixture.native_db) as db:
            db.execute('INSERT INTO session_message VALUES (?,?,?,?,?,?,?)',
                       ('new', self.sid, 'user', 1, int(time.time() * 1000), 1, '{}'))
        with self.assertRaisesRegex(ValueError, 'Native messages'):
            self.reconcile()
        self.assertFalse((self.directory / 'launch-reconciled.json').exists())

    def test_unverifiable_native_timestamp_blocks_no_inference_claim(self):
        with sqlite3.connect(self.fixture.native_db) as db:
            db.execute('INSERT INTO session_message VALUES (?,?,?,?,?,?,?)',
                       ('unknown-time', self.sid, 'assistant', 1, None, 1, '{}'))
        with self.assertRaisesRegex(ValueError, 'Native messages'):
            self.reconcile()

    def test_native_state_or_result_blocks_prelaunch_reconciliation(self):
        for name in ('state.json', 'result.json'):
            target = self.directory / name
            target.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'state/result exists'):
                self.reconcile()
            target.unlink()
        (self.directory / 'state.json').symlink_to(self.directory / 'missing-state')
        with self.assertRaisesRegex(ValueError, 'state/result exists'):
            self.reconcile()

    def test_live_wrapper_or_group_blocks_reconciliation(self):
        with patch.object(opencode.os, 'kill', return_value=None):
            with self.assertRaisesRegex(ValueError, 'PID is still present'):
                self.reconcile()
        with patch.object(opencode, '_group_exists', return_value=True):
            with self.assertRaisesRegex(ValueError, 'group is still present'):
                self.reconcile()

    def test_manager_and_session_locks_block_reconciliation(self):
        with paths.MANAGER_LOCK.open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.reconcile()
        lock = opencode._session_lock(opencode.session_dir(self.sid))
        try:
            with self.assertRaisesRegex(RuntimeError, 'already has a Fleet turn'):
                self.reconcile()
        finally:
            lock.close()

    def test_pause_disabled_slot_and_exact_identity_are_required(self):
        paths.PAUSE.unlink()
        with self.assertRaisesRegex(ValueError, 'durably paused'):
            self.reconcile()
        paths.PAUSE.write_text('paused')
        self.slot['dispatchEnabled'] = True
        with self.assertRaisesRegex(ValueError, 'dispatch-disabled'):
            self.reconcile()
        self.slot['dispatchEnabled'] = False
        request = json.loads(self.request.read_text())
        request['model'] = 'opencode/minimax-m3.1-flash-free'
        self.request.write_text(json.dumps(request))
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            self.reconcile()

    def test_unknown_or_quota_log_is_not_a_prelaunch_guard_proof(self):
        self.log.write_text('HTTP 429: quota exceeded\n')
        with self.assertRaisesRegex(ValueError, 'known pre-provider model guard'):
            self.reconcile()

    def test_active_recovery_reservation_is_never_released(self):
        self.db.execute("INSERT INTO recoveries "
                        "(id,failed_sid,failed_turn,failed_task_id,owner_sid,owner_task_id,"
                        "cwd,branch,head,worktree_sha256,note,status,created,updated) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ('reserved', self.sid, 'other-turn', 'other-failed', self.sid,
                         'other-next', self.identity['cwd'], 'branch', '0' * 40, '0' * 64,
                         'operator hold', 'authorized', 1, 1))
        self.db.commit()
        before = self.db.execute('SELECT * FROM recoveries').fetchall()
        with self.assertRaisesRegex(ValueError, 'Active recovery reservations'):
            self.reconcile()
        self.assertEqual(self.db.execute('SELECT * FROM recoveries').fetchall(), before)
        self.assertFalse((self.directory / 'launch-reconciled.json').exists())

    def test_failed_wrapper_does_not_complete_claimed_recovery_reservation(self):
        self.db.execute("INSERT INTO recoveries "
                        "(id,failed_sid,failed_turn,failed_task_id,owner_sid,owner_task_id,"
                        "cwd,branch,head,worktree_sha256,note,status,created,updated) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ('reserved', self.sid, 'old-turn', 'old-failed', self.sid,
                         'failed-task', self.identity['cwd'], 'branch', '0' * 40, '0' * 64,
                         'operator hold', 'claimed', 1, 1))
        self.db.execute("UPDATE tasks SET state='sent',body=? WHERE id='failed-task'",
                        (json.dumps({'recoveryId': 'reserved'}),))
        self.db.commit()
        manager.observe(self.db, self.sid, opencode.state(self.slot))
        self.assertEqual(self.db.execute("SELECT status FROM recoveries WHERE id='reserved'").fetchone(),
                         ('claimed',))
        self.assertEqual(self.db.execute("SELECT state FROM tasks WHERE id='failed-task'").fetchone(),
                         ('uncertain',))

    def test_database_routing_and_previous_audit_evidence_cannot_change(self):
        self.reconcile()
        self.log.write_text(self.log.read_text().replace('Traceback', 'Changed Traceback'))
        with self.assertRaisesRegex(ValueError, 'Previously audited launch evidence changed'):
            self.reconcile()
        self.request.write_text(json.dumps(dict(self.identity, databasePath='/tmp/other-account.db',
                                                sessionCwd=self.identity['cwd'])))
        with self.assertRaisesRegex(ValueError, 'account/database routing differs'):
            self.reconcile()

    def test_reconciliation_marker_cannot_apply_to_changed_pointer(self):
        self.reconcile()
        launch = json.loads(self.pointer.read_text())
        launch['wrapperPid'] = 99999998
        self.pointer.write_text(json.dumps(launch))
        with self.assertRaisesRegex(RuntimeError, 'reconciliation identity mismatch'):
            opencode.state(self.slot)
