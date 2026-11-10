"""Actual observer processes, disposable repositories, and the existing fake API."""
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from dispatch import Dispatcher, DispatchError
from runner import inspect_run, observer_lock, request_stop, run
import test_batch


class RunnerTests(unittest.TestCase):
    git = test_batch.BatchTests.git
    approve = test_batch.BatchTests.approve
    commit = test_batch.BatchTests.commit
    configure = test_batch.BatchTests.configure
    enqueue = test_batch.BatchTests.enqueue
    second_session = test_batch.BatchTests.second_session

    def setUp(self):
        test_batch.BatchTests.setUp(self)
        self.policy['wait_seconds'] = 0
        self.run_dir = self.root / 'run'
        self.children, self.logs = [], []

    def tearDown(self):
        if (self.run_dir / 'state.json').exists():
            request_stop(self.ledger, self.run_dir)
        for child in self.children:
            if child.poll() is None:
                child.terminate()  # Only this test's owned Popen child; no agent signal.
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        for log in self.logs:
            log.close()
        # The single detached-launch fixture waits for its own exit receipt before cleanup.
        test_batch.BatchTests.tearDown(self)

    def args(self, command='run', *extra, run_dir=None):
        return [sys.executable, str(Path(__file__).with_name('runner.py')), '--db', str(self.ledger),
                command, '--run-dir', str(run_dir or self.run_dir), *extra]

    def spawn(self, *extra):
        log = (self.root / ('observer-' + str(len(self.children)) + '.log')).open('w')
        self.logs.append(log)
        child = subprocess.Popen(self.args('run', *extra), stdout=log, stderr=subprocess.STDOUT)
        self.children.append(child)
        return child

    def wait_for(self, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail('Fixture condition did not become observable before deadline')

    def state(self):
        path = self.run_dir / 'state.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def test_killed_observer_restarts_from_cursor_without_duplicate_dispatch_and_drains_past_cutoff(self):
        self.enqueue()
        self.second_session()
        self.batch.enqueue('doc-2', 'second', 'Wait independently.', ['README.md'], self.policy)
        child = self.spawn('--seconds', '30', '--interval', '1')
        self.wait_for(lambda: self.state().get('ticks', 0) >= 1 and len(self.api.posts) == 1)
        prior = self.state()
        self.assertEqual(prior['owner']['pid'], child.pid)
        if 'start_ticks' in prior['owner']:
            actual = Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
            self.assertEqual(actual, prior['owner']['start_ticks'])
        child.kill()
        child.wait(timeout=5)
        self.assertEqual(inspect_run(self.ledger, run_dir=self.run_dir)['state'], 'unobserved_exit')
        request_stop(self.ledger, self.run_dir)  # An old attempt's stop request cannot stop its successor.
        cursor = self.dispatcher.task('doc-1')['cursor']
        commit = self.commit()
        self.api.finish()
        self.until = (self.at - timedelta(seconds=1)).isoformat()  # Existing pool cutoff, not runner deadline.
        self.configure()
        resumed = self.spawn()  # Original run deadline/interval are retained.
        self.assertEqual(resumed.wait(timeout=15), 2)
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.dispatcher.task('doc-1')['verification'] is not None, True)
        self.assertGreater(self.dispatcher.task('doc-1')['cursor'], cursor)
        self.assertEqual(self.life.unit('doc-1')['detail']['final_commit'], commit)
        self.assertEqual(self.life.unit('doc-1')['detail']['learning'], 'skipped_gpu_unavailable')
        self.assertEqual(self.batch.status()['tasks'][1]['state'], 'pending')
        final = self.state()
        self.assertNotEqual(prior['attempt'], final['attempt'])
        self.assertEqual(prior['deadline'], final['deadline'])
        self.assertIn('batch.py', final['code'])
        self.assertEqual(final['schema_at_start'], 3)

    def test_detached_launch_survives_launcher_and_stop_does_not_cancel_agents_or_pause_pool(self):
        self.enqueue()
        launch = subprocess.run(self.args('start', '--seconds', '30', '--interval', '1'), capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        meta = json.loads(launch.stdout)
        self.assertNotEqual(meta['owner']['pid'], os.getpid())
        self.wait_for(lambda: self.state().get('ticks', 0) >= 2)
        self.api.sessions['ses_fixture']['active'] = True
        duplicate = subprocess.run(self.args('run', '--seconds', '1', run_dir=self.root / 'other-run'),
                                   capture_output=True, text=True, timeout=5)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn('Another observer', duplicate.stderr)
        legacy = subprocess.run([sys.executable, str(Path(__file__).with_name('batch.py')), '--db', str(self.ledger),
                                 'run', '--seconds', '0'], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(legacy.returncode, 0)
        self.assertIn('Another observer', legacy.stderr)
        request_stop(self.ledger, self.run_dir)
        self.wait_for(lambda: self.state().get('status') == 'exited')
        self.assertEqual(self.state()['exit_reason'], 'observer_stop_requested')
        self.assertTrue(self.api.sessions['ses_fixture']['active'])
        self.assertEqual(self.batch.pool('gpu')['state'], 'available')
        self.assertEqual(len(self.api.posts), 1)

    def test_expired_observer_deadline_never_admits_or_gets_extended_on_restart(self):
        self.enqueue()
        first = self.spawn('--seconds', '0')
        self.assertEqual(first.wait(timeout=5), 2)
        deadline = self.state()['deadline']
        again = self.spawn()
        self.assertEqual(again.wait(timeout=5), 2)
        self.assertEqual(self.state()['deadline'], deadline)
        refused = subprocess.run(self.args('run', '--seconds', '60'), capture_output=True, text=True, timeout=5)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.api.posts, [])

    def test_sigterm_stops_only_the_observer_at_a_tick_boundary(self):
        self.enqueue()
        child = self.spawn('--seconds', '30', '--interval', '1')
        self.wait_for(lambda: self.state().get('ticks', 0) >= 1)
        self.api.sessions['ses_fixture']['active'] = True
        child.terminate()
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertEqual(self.state()['exit_reason'], 'observer_stop_requested')
        self.assertTrue(self.api.sessions['ses_fixture']['active'])
        self.assertEqual(len(self.api.posts), 1)

    def test_state_is_refused_inside_a_registered_worktree(self):
        target = self.repo / 'runner-state'
        with self.assertRaisesRegex(DispatchError, 'outside registered worktrees'):
            run(self.ledger, run_dir=target, seconds=0)
        self.assertFalse(target.exists())

    def test_basic_ledger_is_not_silently_opted_into_queue_schema(self):
        path = self.root / 'basic.sqlite3'
        basic = Dispatcher(path)
        basic.close()
        before = path.read_bytes()
        with self.assertRaisesRegex(DispatchError, 'supported queue ledger required'):
            run(path, run_dir=self.root / 'basic-run', seconds=0)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / 'basic-run').exists())

    def test_parent_directory_alias_cannot_bypass_observer_ownership(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.ledger.parent, target_is_directory=True)
        with observer_lock(self.ledger):
            with self.assertRaisesRegex(DispatchError, 'Another observer'):
                with observer_lock(alias / self.ledger.name):
                    self.fail('Duplicate owner admitted through an alias')

    def test_independent_work_drains_around_a_held_assignment(self):
        self.configure(capacity=2)
        self.enqueue()
        self.batch.tick(self.at)
        self.api.finish(terminal=None)
        self.api.append('ses_fixture', 'session.execution.failed')
        self.batch.tick(self.at)
        other = self.second_session()
        self.batch.enqueue('doc-2', 'second', 'Finish independently.', ['README.md'], self.policy)
        child = self.spawn('--seconds', '30', '--interval', '1')
        self.wait_for(lambda: self.state().get('ticks', 0) >= 1 and len(self.api.posts) == 2)
        (other / 'README.md').write_text('Verified independently.\n')
        self.git('add', 'README.md', path=other)
        self.git('commit', '-m', 'Independent result\n\nOpenCode-Task: doc-2', path=other)
        self.api.finish('ses_second')
        self.until = (self.at - timedelta(seconds=1)).isoformat()
        self.configure(capacity=2)
        self.assertEqual(child.wait(timeout=15), 1)
        states = {r['task_id']: r['state'] for r in self.batch.status()['tasks']}
        self.assertEqual(states, {'doc-1': 'blocked', 'doc-2': 'ready'})
        self.assertEqual(len(self.api.posts), 2)


if __name__ == '__main__':
    unittest.main()
