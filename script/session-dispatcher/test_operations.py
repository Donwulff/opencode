"""Read-only views against disposable Git, the real ledger, and loopback API."""
from datetime import datetime, timedelta, timezone
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest

from dispatch import Client, DispatchError
from operations import report
from runner import inspect_run, observer_lock, write_json
import test_batch


class OperationsTests(unittest.TestCase):
    git = test_batch.BatchTests.git
    approve = test_batch.BatchTests.approve
    commit = test_batch.BatchTests.commit
    tearDown = test_batch.BatchTests.tearDown
    configure = test_batch.BatchTests.configure
    enqueue = test_batch.BatchTests.enqueue
    second_session = test_batch.BatchTests.second_session
    task_done = test_batch.BatchTests.task_done
    setUp = test_batch.BatchTests.setUp

    def test_offline_view_never_mutates_or_echoes_private_payloads(self):
        self.batch.enqueue('doc-1', 'worker', 'PRIVATE PROMPT email@example.invalid secret', ['README.md'], self.policy)
        before = hashlib.sha256(self.ledger.read_bytes()).hexdigest()
        value = report(self.ledger)
        self.assertEqual(hashlib.sha256(self.ledger.read_bytes()).hexdigest(), before)
        self.assertEqual(self.dispatcher.db.execute('pragma user_version').fetchone()[0], 3)
        self.assertEqual(self.api.posts, [])
        rendered = json.dumps(value)
        self.assertNotIn('PRIVATE PROMPT', rendered)
        self.assertNotIn('email@example.invalid', rendered)
        self.assertNotIn('disposable-fixture', rendered)
        self.assertEqual(value['pools'][0]['occupancy'], 'unknown')
        self.assertIsNone(value['pools'][0]['available_slots'])
        self.assertEqual(value['runner']['state'], 'unmanaged_or_no_record')

    def test_live_union_explains_holds_and_counts_forms_without_copying_content(self):
        self.enqueue()
        self.batch.tick(self.at)
        self.api.sessions['ses_fixture']['active'] = True
        self.api.sessions['ses_fixture']['forms'] = [{'question': 'PRIVATE QUESTION secret'}]
        value = report(self.ledger, live=True)
        pool = value['pools'][0]
        self.assertEqual(pool['occupied'], 1)  # Native execution and ledger hold are the same session.
        self.assertEqual({r['kind'] for r in pool['members']['ses_fixture']}, {'native_active', 'unresolved_task'})
        self.assertEqual(value['native_sessions'][0]['form_count'], 1)
        self.assertNotIn('PRIVATE QUESTION', json.dumps(value))
        self.assertEqual(len(self.api.posts), 1)

    def test_learning_is_separate_and_keeps_its_exact_reservation(self):
        self.task_done()
        unit = self.life.unit('doc-1')
        value = report(self.ledger, live=True)
        self.assertEqual(value['tasks'][0]['stage'], 'learning')
        reasons = value['pools'][0]['members'][unit['fork_id']]
        self.assertIn({'kind': 'learning_unit', 'task_id': 'doc-1', 'stage': 'learning'}, reasons)
        self.assertEqual(value['pools'][0]['occupied'], 1)
        self.assertEqual(len(self.api.posts), 2)

    def test_failed_probe_keeps_capacity_unknown_and_does_not_follow_redirect(self):
        self.enqueue()
        self.api.redirect = True
        value = report(self.ledger, live=True)
        self.assertEqual(value['pools'][0]['occupancy'], 'unknown')
        self.assertIsNone(value['pools'][0]['available_slots'])
        self.assertEqual(value['native_sessions'][0]['state'], 'unknown')
        self.assertEqual(self.api.captured, 0)
        self.assertEqual(self.api.posts, [])

    def test_slow_native_probe_does_not_hold_a_live_ledger_reader_lock(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        class SlowClient(Client):
            def json(self, method, path, payload=None):
                if path == '/api/session/active':
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('Fixture did not release its GET barrier')
                return super().json(method, path, payload)
        thread = threading.Thread(target=lambda: results.append(report(self.ledger, live=True, client_factory=SlowClient)))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            # A committing writer must not wait for the blocked HTTP request.
            self.dispatcher.db.execute('PRAGMA busy_timeout=100')
            self.dispatcher.db.execute("UPDATE pool SET external=1 WHERE name='gpu'")
        finally:
            release.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]['pools'][0]['external'], 0)  # Explicitly the snapshot, not the later write.

    def test_ready_receipt_is_historical_without_a_fresh_git_claim(self):
        self.policy['wait_seconds'] = 0
        self.enqueue()
        self.batch.tick(self.at)
        commit = self.commit()
        self.api.finish()
        self.configure(state='paused')
        self.batch.tick(self.at)
        (self.repo / 'unrelated.txt').write_text('later work\n')
        value = report(self.ledger)
        self.assertEqual(value['ready_receipts'][0]['final_commit'], commit)
        self.assertIn('not_fresh', value['ready_receipts'][0]['meaning'])

    def test_unrelated_database_is_refused_without_changes(self):
        path = self.root / 'other.sqlite'
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table unrelated(value)')
        path.chmod(0o600)
        before = path.read_bytes()
        with self.assertRaisesRegex(DispatchError, 'Not a dispatcher'):
            report(path)
        self.assertEqual(path.read_bytes(), before)

    def test_dependency_schema_and_legacy_specs_remain_unchanged_in_cli_view(self):
        self.enqueue()
        self.second_session()
        self.batch.enqueue('doc-2', 'second', 'Dependent fixture.', ['README.md'], self.policy,
                           [{'task': 'doc-1', 'when': 'verified'}])
        before = self.ledger.read_bytes()
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('batch.py')), '--db', str(self.ledger),
                                 'status', '--operations'], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['schema'], 6)
        self.assertEqual(value['tasks'][1]['dependencies'], [{'task': 'doc-1', 'when': 'verified'}])
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.api.posts, [])

    def test_accounted_failure_keeps_history_without_being_presented_as_a_current_hold(self):
        self.enqueue()
        self.batch.tick(self.at)
        self.api.finish(terminal=None)
        self.api.append('ses_fixture', 'session.execution.failed')
        self.batch.tick(self.at)
        self.dispatcher.update('doc-1', state='accounted')
        self.life.save('doc-1', 'ready', learning='manual_closeout', final_commit=self.base)
        self.batch.save('doc-1', 'deferred', 'Original failure retained after explicit manual closeout')
        value = report(self.ledger, live=True)
        row = value['tasks'][0]
        self.assertTrue(row['has_hazard'])
        self.assertFalse(row['worktree_reserved'])
        self.assertEqual(row['hold_reason'], 'historical_accounted')
        self.assertEqual(value['pools'][0]['occupied'], 0)
        self.assertFalse(value['ready_receipts'][0]['verified_delivery'])

    def test_stale_heartbeat_is_unknown_even_with_lock_and_plausible_pid(self):
        root = self.root / 'runner'
        root.mkdir(mode=0o700)
        at = datetime.now(timezone.utc)
        state = {'database': str(self.ledger.absolute()), 'attempt': 'fixture_attempt', 'interval': 1,
                 'heartbeat_at': (at - timedelta(minutes=5)).isoformat(), 'status': 'observing',
                 'owner': {'pid': 1}}
        write_json(root / 'state.json', state)
        with observer_lock(self.ledger) as handle:
            handle.write(json.dumps({'attempt': state['attempt']}))
            handle.flush()
            value = inspect_run(self.ledger, run_dir=root, at=at)
            self.assertEqual((value['state'], value['ownership']), ('unknown', 'held'))
        self.assertEqual(inspect_run(self.ledger, run_dir=root, at=at)['state'], 'unobserved_exit')


if __name__ == '__main__':
    unittest.main()
