"""Disposable Git and loopback API fixtures; no live sessions, models, or containers."""
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from dispatch import Dispatcher, DispatchError, encoded
from batch import Batch
from recovery import Recovery
import test_lifecycle


class BatchTests(unittest.TestCase):
    git = test_lifecycle.LifecycleTests.git
    approve = test_lifecycle.LifecycleTests.approve
    commit = test_lifecycle.LifecycleTests.commit
    tearDown = test_lifecycle.LifecycleTests.tearDown

    def setUp(self):
        test_lifecycle.LifecycleTests.setUp(self)
        self.batch = Batch(self.dispatcher)
        self.at = datetime.now(timezone.utc)
        self.until = (self.at + timedelta(hours=1)).isoformat()
        self.configure()

    def configure(self, state='available', capacity=1, external=0):
        return self.batch.configure('gpu', self.api.connection, 'fixture', capacity, state, self.until, external)

    def enqueue(self, task='doc-1', worker='worker'):
        return self.batch.enqueue(task, worker, 'Change pending to verified.', ['README.md'], self.policy)

    def second(self):
        repo = self.root / 'second'
        self.git('worktree', 'add', '-b', 'second', str(repo))
        self.api.session(repo, 'ses_second')
        self.approve('second', 'ses_second', repo)
        self.enqueue('doc-2', 'second')

    def second_session(self, name='second', sid='ses_second'):
        repo = self.root / name
        self.git('worktree', 'add', '-b', name, str(repo))
        self.api.session(repo, sid)
        self.approve(name, sid, repo)
        return repo

    def deferred_stop(self, supersede=True, prepare=True, queue='deferred'):
        """One retained, deferred stopped attempt: an attributable terminal failure before
        any tool event, an idle session with a settled retained tail, one continuation
        intent the coordinator superseded, the record explicitly deferred out of the
        queue's active set, and a worktree that later advanced externally. Nothing resumes
        or edits it. queue=None drops the fixture queue row to model a missing record."""
        self.batch.enqueue('old-1', 'worker', 'Change pending to verified.', ['README.md'], self.policy)
        self.batch.tick(self.at)
        self.api.finish(terminal=None)
        self.api.append('ses_fixture', 'session.execution.failed')
        self.batch.tick(self.at)
        task = self.dispatcher.task('old-1')
        self.assertEqual((task['state'], task['terminal'], task['enqueued'], task['delivered'],
                          task['issue'], task['hazard']),
                         ('uncertain', 'failed', 1, 1, None, 'Execution failed/interrupted; effects may already exist'))
        if prepare:
            recovery = Recovery(self.dispatcher)
            model = json.loads(task['registry'])['model']
            plan = recovery.plan('old-1', model)
            recovery.prepare('old-1', model, plan['approval_hash'])
            if supersede:
                recovery.save('old-1', 1, 'superseded')
        if queue is None:
            self.dispatcher.db.execute("DELETE FROM queued WHERE task_id='old-1'")
        else:
            self.batch.save('old-1', queue, 'Deferred: replacement verified and merged separately')
        (self.repo / 'HANDOVER.md').write_text('Coordinator checkpoint.\n')
        self.git('add', 'HANDOVER.md')
        self.git('commit', '-m', 'Coordinator checkpoint')
        return dict(self.dispatcher.task('old-1'))

    def deferred_capacity(self, mutate, stop):
        self.deferred_stop(**stop)
        mutate()
        self.second_session()
        self.batch.enqueue('new-1', 'second', 'Change pending to verified.', ['README.md'], self.policy)
        result = self.batch.tick(self.at)
        self.assertEqual(result['phase'], 'paused', result)
        self.assertEqual(result['waiting'], {'new-1': 'capacity'}, result)
        self.assertEqual([row['state'] for row in result['tasks'] if row['task_id'] == 'new-1'], ['pending'])
        self.assertEqual(len(self.api.posts), 1)  # The waiting task never receives an inference request.

    def task_done(self):
        self.enqueue()
        self.batch.tick(self.at)
        self.commit()
        self.api.finish()
        result = self.batch.tick(self.at)
        self.assertEqual(self.life.unit('doc-1')['stage'], 'learning', result)

    def test_durable_queue_idempotency_and_model_free_admission(self):
        self.enqueue()
        self.enqueue()
        self.assertEqual(self.api.posts, [])
        self.batch.tick(self.at)
        self.dispatcher.close()
        self.dispatcher = Dispatcher(self.ledger, lambda _: test_lifecycle.ForkClient(self.api))
        self.batch = Batch(self.dispatcher)
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.dispatcher.db.execute('pragma user_version').fetchone()[0], 3)
        with self.assertRaisesRegex(DispatchError, 'another queued'):
            self.batch.enqueue('doc-1', 'worker', 'Different task', ['README.md'], self.policy)

    def test_capacity_then_learning_then_refill(self):
        self.enqueue()
        self.second()
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts), 1)
        self.commit()
        self.api.finish()
        self.batch.tick(self.at)  # Verification and priority admission for learning.
        unit = self.life.unit('doc-1')
        self.assertEqual(unit['stage'], 'learning', unit)
        self.api.finish(unit['fork_id'])
        result = self.batch.tick(self.at)
        self.assertEqual(result['tasks'][0]['state'], 'ready', result)
        self.assertEqual(self.life.unit('doc-1')['detail']['learning'], 'completed_no_changes')
        self.assertEqual(self.dispatcher.task('doc-2')['state'], 'admitted')
        self.assertEqual(len(self.api.posts), 3)  # Two tasks and one learning prompt, once each.

    def test_outside_window_and_external_occupancy(self):
        self.enqueue()
        result = self.batch.tick(self.at + timedelta(hours=2))
        self.assertEqual(result['waiting']['doc-1'], 'outside_window')
        self.configure(external=1)
        self.assertEqual(self.batch.tick(self.at)['waiting']['doc-1'], 'capacity')
        self.assertEqual(self.api.posts, [])
        self.configure(state='quota')
        self.assertEqual(self.batch.tick(self.at)['waiting']['doc-1'], 'quota')

    def test_external_live_sessions_count(self):
        sid = self.api.session(self.repo, 'ses_external')
        self.api.sessions[sid]['active'] = True
        self.enqueue()
        self.assertEqual(self.batch.tick(self.at)['waiting']['doc-1'], 'capacity')
        self.assertEqual(self.api.posts, [])

    def test_window_closes_without_cancelling_active_execution(self):
        self.enqueue()
        self.batch.tick(self.at)
        self.api.sessions['ses_fixture']['active'] = True
        result = self.batch.tick(self.at + timedelta(hours=2))
        self.assertEqual(result['phase'], 'draining')
        self.assertEqual(result['waiting']['doc-1'], 'outside_window')
        self.assertEqual(result['tasks'][0]['state'], 'active')
        self.assertTrue(self.api.sessions['ses_fixture']['active'])
        self.assertEqual(len(self.api.posts), 1)

    def test_task_completion_after_cutoff_is_verified_without_new_admissions(self):
        self.policy['wait_seconds'] = 0
        self.enqueue()
        self.second()
        self.batch.tick(self.at)
        self.commit()
        self.api.finish()
        result = self.batch.tick(self.at + timedelta(hours=2))
        self.assertEqual(result['phase'], 'paused')
        self.assertEqual({row['task_id']: row['state'] for row in result['tasks']},
                         {'doc-1': 'ready', 'doc-2': 'pending'})
        self.assertEqual(self.dispatcher.task('doc-1')['state'], 'verified')
        self.assertEqual(self.life.unit('doc-1')['detail']['learning'], 'skipped_gpu_unavailable')
        self.assertEqual(self.api.forks, 0)
        self.assertEqual(len(self.api.posts), 1)

    def test_already_running_learning_finishes_after_cutoff_without_another_prompt(self):
        self.task_done()
        unit = self.life.unit('doc-1')
        self.api.sessions[unit['fork_id']]['active'] = True
        closed = self.at + timedelta(hours=2)
        self.assertEqual(self.batch.tick(closed)['phase'], 'draining')
        self.api.finish(unit['fork_id'])
        self.assertEqual(self.batch.tick(closed)['phase'], 'complete')
        self.assertEqual(self.life.unit('doc-1')['detail']['learning'], 'completed_no_changes')
        self.assertEqual(len(self.api.posts), 2)

    def test_closed_pool_states_do_not_stop_observation_or_reopen_admissions(self):
        self.enqueue()
        self.batch.tick(self.at)
        for state in ('paused', 'quota', 'limited', 'offline'):
            with self.subTest(state=state):
                self.configure(state=state)
                result = self.batch.tick(self.at)
                self.assertEqual(result['phase'], 'draining')
                self.assertEqual(result['waiting']['doc-1'], state)
                self.assertEqual(self.batch.pool('gpu')['state'], state)
                self.assertEqual(len(self.api.posts), 1)

    def test_unsubmitted_learning_prompt_waits_for_an_open_pool_after_interruption(self):
        self.enqueue()
        self.batch.tick(self.at)
        self.commit()
        self.api.finish()
        with patch.object(self.dispatcher, 'submit', side_effect=SystemExit('Fixture interruption before prompt')):
            with self.assertRaises(SystemExit):
                self.batch.tick(self.at)
        unit = self.life.unit('doc-1')
        self.assertEqual(unit['stage'], 'learning')
        self.assertFalse(self.dispatcher.db.execute('SELECT 1 FROM task WHERE id=?', (unit['child_id'],)).fetchone())
        self.assertEqual(self.batch.tick(self.at + timedelta(hours=2))['phase'], 'draining')
        self.assertEqual(len(self.api.posts), 1)
        self.batch.tick(self.at)
        self.assertEqual(self.api.forks, 1)
        self.assertEqual(len(self.api.posts), 2)

    def test_cli_keeps_observing_past_cutoff_until_active_work_is_accounted(self):
        self.policy['wait_seconds'] = 0
        self.enqueue()
        self.second()
        self.batch.tick(self.at)
        self.until = (self.at - timedelta(seconds=1)).isoformat()
        self.configure()
        command = [sys.executable, str(Path(__file__).with_name('batch.py')), '--db', str(self.ledger),
                   'run', '--seconds', '10', '--interval', '1']
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        try:
            first = json.loads(process.stdout.readline())
            self.assertEqual(first['phase'], 'draining')
            self.commit()
            self.api.finish()
            output, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 2, error)
            self.assertEqual(json.loads(output.splitlines()[-1])['phase'], 'paused')
            self.assertEqual(self.life.unit('doc-1')['stage'], 'ready')
            self.assertEqual(len(self.api.posts), 1)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_cli_observer_deadline_still_stops_with_active_work_preserved(self):
        self.enqueue()
        self.batch.tick(self.at)
        self.until = (self.at - timedelta(seconds=1)).isoformat()
        self.configure()
        result = self.run_cli()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['phase'], 'draining')
        self.assertEqual(self.batch.status()['tasks'][0]['state'], 'active')
        self.assertEqual(len(self.api.posts), 1)

    def test_duplicate_worktree_pending_assignment_refused(self):
        self.enqueue()
        with self.assertRaisesRegex(DispatchError, 'reserves'):
            self.enqueue('doc-2')

    def test_worktree_drift_refused_before_post(self):
        self.enqueue()
        (self.repo / 'README.md').write_text('Concurrent work.\n')
        self.assertEqual(self.batch.tick(self.at)['phase'], 'blocked')
        self.assertEqual(self.api.posts, [])

    def test_queued_template_or_registry_changes_require_review(self):
        self.enqueue()
        Path(self.policy['prompt']).write_text('Changed learn prompt.\n')
        self.assertEqual(self.batch.tick(self.at)['phase'], 'blocked')
        self.assertEqual(self.api.posts, [])

    def test_interrupted_admission_intent_is_never_retried(self):
        self.enqueue()
        self.batch.save('doc-1', 'active')
        result = self.batch.tick(self.at)
        self.assertEqual(result['phase'], 'blocked')
        self.assertIn('Admission intent', result['tasks'][0]['issue'])
        self.batch.tick(self.at)
        self.assertEqual(self.api.posts, [])

    def test_lost_receipt_and_observer_reconnect_never_resend(self):
        self.enqueue()
        self.api.lose_receipt = True
        # The unconfirmed admission holds new admissions, but the already-admitted unit
        # keeps being drained/observed instead of stopping the observer.
        self.assertEqual(self.batch.tick(self.at)['phase'], 'draining')
        self.api.cut_after = 0
        self.assertEqual(self.batch.tick(self.at)['phase'], 'draining')
        self.api.cut_after = None
        self.assertEqual(self.batch.tick(self.at)['phase'], 'running')
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.dispatcher.task('doc-1')['state'], 'admitted')

    def failed(self, error):
        self.enqueue()
        self.second()
        self.batch.tick(self.at)
        self.api.finish(terminal=None)
        self.api.append('ses_fixture', 'session.step.failed', error=error)
        self.api.append('ses_fixture', 'session.execution.failed')
        result = self.batch.tick(self.at)
        self.assertEqual(result['phase'], 'blocked')
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts), 1)
        return result

    def test_provider_quota_pauses_pool_without_disclosing_error_body(self):
        result = self.failed({'type': 'provider.free-limit', 'message': 'private error', 'retryAfterMs': 1000})
        self.assertEqual(self.batch.pool('gpu')['state'], 'quota')
        self.assertNotIn('private error', encoded(result))

    def test_rate_limit_is_not_classified_as_daily_quota(self):
        self.failed({'type': 'provider.rate-limit', 'status': 429, 'message': 'rate limited'})
        self.assertEqual(self.batch.pool('gpu')['state'], 'limited')

    def test_tool_http_error_does_not_mark_inference_pool_exhausted(self):
        self.failed({'type': 'tool.execution', 'status': 429, 'message': 'remote tool rate limited'})
        self.assertEqual(self.batch.pool('gpu')['state'], 'available')

    def test_backend_failure_holds_uncertain_effects(self):
        self.failed({'type': 'provider.api', 'status': 503, 'message': 'backend unavailable'})
        self.assertEqual(self.life.unit('doc-1')['stage'], 'needs_review')
        self.assertEqual(self.batch.pool('gpu')['state'], 'available')

    def test_learning_uses_independent_model_and_respects_its_pool(self):
        self.api.sessions['ses_fixture']['info']['model']['providerID'] = 'primary'
        self.approve()
        self.batch.configure('primary', self.api.connection, 'primary', 1, 'available', self.until)
        self.enqueue()
        self.batch.tick(self.at)
        self.commit()
        self.api.finish()
        self.configure(state='paused')
        self.batch.tick(self.at)
        self.assertEqual(self.api.forks, 0)
        self.configure()
        self.batch.tick(self.at)
        fork = self.life.unit('doc-1')['fork_id']
        self.assertEqual(self.api.sessions[fork]['info']['model'], self.policy['model'])
        self.assertEqual(self.api.sessions['ses_fixture']['info']['model']['providerID'], 'primary')

    def test_queue_and_existing_lifecycle_share_lock(self):
        self.enqueue()
        with self.ledger.open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(DispatchError, 'writer holds'):
                self.batch.tick(self.at)
        self.assertEqual(self.api.posts, [])

    def test_no_learning_changes_finishes_without_repeating_or_merging(self):
        self.task_done()
        self.batch.tick(self.at)
        unit = self.life.unit('doc-1')
        self.api.finish(unit['fork_id'])
        self.assertEqual(self.batch.tick(self.at)['phase'], 'complete')
        self.assertEqual(self.batch.tick(self.at)['phase'], 'complete')
        self.assertEqual(self.api.forks, 1)
        self.assertEqual(len(self.api.posts), 2)
        self.assertEqual(self.batch.status()['integration'], 'human_review_required')

    def test_held_assignment_drains_other_admitted_units_without_new_admissions(self):
        self.configure(capacity=2)
        self.enqueue()
        self.second()
        self.second_session('third', 'ses_third')
        self.batch.enqueue('doc-3', 'third', 'Change pending to verified.', ['README.md'], self.policy)
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts), 2)  # doc-1/doc-2 admitted; doc-3 waits on capacity.
        # doc-1 hits an attributable backend failure and is held for review.
        self.api.finish('ses_fixture', terminal=None)
        self.api.append('ses_fixture', 'session.step.failed',
                        error={'type': 'provider.api', 'status': 503, 'message': 'backend unavailable'})
        self.api.append('ses_fixture', 'session.execution.failed')
        result = self.batch.tick(self.at)
        self.assertEqual(result['phase'], 'draining', result)
        self.assertEqual(len(self.api.posts), 2)  # The still-pending doc-3 receives no prompt.
        self.assertEqual(self.life.unit('doc-1')['stage'], 'needs_review')
        original = dict(self.dispatcher.task('doc-1'))
        queued = dict(self.dispatcher.db.execute("SELECT * FROM queued WHERE task_id='doc-1'").fetchone())
        unit = dict(self.dispatcher.db.execute("SELECT * FROM unit WHERE task_id='doc-1'").fetchone())
        observed = [tuple(row) for row in self.dispatcher.db.execute(
            "SELECT * FROM observed WHERE task_id='doc-1' ORDER BY seq")]
        # The unaffected doc-2 commits and completes; its enrolled learning forks exactly once.
        second = self.root / 'second'
        (second / 'README.md').write_text('Task status: verified.\n')
        self.git('add', 'README.md', path=second)
        self.git('commit', '-m', 'docs: fixture result\n\nOpenCode-Task: doc-2', path=second)
        self.api.finish('ses_second')
        result = self.batch.tick(self.at)
        self.assertEqual(result['phase'], 'draining', result)
        self.assertEqual(self.life.unit('doc-2')['stage'], 'learning', result)
        self.assertEqual(self.api.forks, 1)
        self.assertEqual(len(self.api.posts), 3)
        self.api.finish(self.life.unit('doc-2')['fork_id'])
        # All unaffected units are settled: stop non-successfully on the remaining hold.
        result = self.batch.tick(self.at)
        self.assertEqual(result['phase'], 'blocked', result)
        self.assertEqual(self.life.unit('doc-2')['stage'], 'ready')
        self.assertEqual(self.life.unit('doc-2')['detail']['learning'], 'completed_no_changes')
        self.assertEqual(self.dispatcher.task(self.life.unit('doc-2')['child_id'])['state'], 'accounted')
        self.assertEqual([row['state'] for row in result['tasks'] if row['task_id'] == 'doc-3'], ['pending'])
        self.assertEqual(len(self.api.posts), 3)  # doc-3 never receives a prompt.
        # The held record and its worktree/evidence stay intact.
        self.assertEqual(dict(self.dispatcher.task('doc-1')), original)
        self.assertEqual(dict(self.dispatcher.db.execute("SELECT * FROM queued WHERE task_id='doc-1'").fetchone()), queued)
        self.assertEqual(dict(self.dispatcher.db.execute("SELECT * FROM unit WHERE task_id='doc-1'").fetchone()), unit)
        self.assertEqual([tuple(row) for row in self.dispatcher.db.execute(
            "SELECT * FROM observed WHERE task_id='doc-1' ORDER BY seq")], observed)
        with self.assertRaisesRegex(DispatchError, 'reserves'):
            self.approve()
        with self.assertRaisesRegex(DispatchError, 'unresolved'):
            self.dispatcher.submit('doc-4', 'worker', 'Change pending to verified.', ['README.md'])
        self.assertEqual(len(self.api.posts), 3)
        # An observer reconnect drains nothing twice: no repeated task or learning delivery.
        self.dispatcher.close()
        self.dispatcher = Dispatcher(self.ledger, lambda _: test_lifecycle.ForkClient(self.api))
        self.batch = Batch(self.dispatcher)
        self.assertEqual(self.batch.tick(self.at)['phase'], 'blocked')
        self.assertEqual(len(self.api.posts), 3)
        self.assertEqual(self.api.forks, 1)

    def test_cli_drains_other_admitted_units_then_stops_blocked_on_the_hold(self):
        # The loopback fixture implements fork/model/PATCH only in-process, so the
        # subprocess observer drains under a closed learning window instead: the
        # unaffected unit's learning is bounded wait/skip, and the CLI-level contract
        # is drain the admitted work, then stop non-successfully on the hold.
        self.policy['wait_seconds'] = 0
        self.configure(capacity=2)
        self.enqueue()
        self.second()
        self.second_session('third', 'ses_third')
        self.batch.enqueue('doc-3', 'third', 'Change pending to verified.', ['README.md'], self.policy)
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts), 2)  # doc-1/doc-2 admitted; doc-3 waits.
        # doc-1 hits an attributable backend failure and is held for review.
        self.api.finish('ses_fixture', terminal=None)
        self.api.append('ses_fixture', 'session.step.failed',
                        error={'type': 'provider.api', 'status': 503, 'message': 'backend unavailable'})
        self.api.append('ses_fixture', 'session.execution.failed')
        self.until = (self.at - timedelta(seconds=1)).isoformat()
        self.configure(capacity=2)
        command = [sys.executable, str(Path(__file__).with_name('batch.py')), '--db', str(self.ledger),
                   'run', '--seconds', '20', '--interval', '1']
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        try:
            first = json.loads(process.stdout.readline())
            self.assertEqual(first['phase'], 'draining', first)
            second = self.root / 'second'
            (second / 'README.md').write_text('Task status: verified.\n')
            self.git('add', 'README.md', path=second)
            self.git('commit', '-m', 'docs: fixture result\n\nOpenCode-Task: doc-2', path=second)
            self.api.finish('ses_second')
            output, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 1, error)
            self.assertEqual(json.loads(output.splitlines()[-1])['phase'], 'blocked')
            self.assertEqual(len(self.api.posts), 2)  # doc-3 never receives a prompt while the hold stands.
            self.assertEqual(self.api.forks, 0)
            unit = self.life.unit('doc-2')
            self.assertEqual(unit['stage'], 'ready', unit)
            self.assertEqual(unit['detail']['learning'], 'skipped_gpu_unavailable')
            self.assertEqual(self.life.unit('doc-1')['stage'], 'needs_review')
            self.assertEqual({row['task_id']: row['state'] for row in self.batch.status()['tasks']},
                             {'doc-1': 'blocked', 'doc-2': 'ready', 'doc-3': 'pending'})
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_settled_superseded_stop_frees_exactly_one_inference_slot(self):
        original = self.deferred_stop()
        attempt = dict(self.dispatcher.db.execute("SELECT * FROM attempt WHERE task_id='old-1'").fetchone())
        queued = dict(self.dispatcher.db.execute("SELECT * FROM queued WHERE task_id='old-1'").fetchone())
        unit = dict(self.dispatcher.db.execute("SELECT * FROM unit WHERE task_id='old-1'").fetchone())
        observed = [tuple(row) for row in self.dispatcher.db.execute(
            "SELECT * FROM observed WHERE task_id='old-1' ORDER BY seq")]
        # The deferred stop no longer occupies the provider's sole inference slot.
        self.assertIsNone(self.batch.availability('gpu', self.at))
        self.second_session()
        self.batch.enqueue('new-1', 'second', 'Change pending to verified.', ['README.md'], self.policy)
        self.second_session('third', 'ses_third')
        self.batch.enqueue('new-2', 'third', 'Change pending to verified.', ['README.md'], self.policy)
        result = self.batch.tick(self.at)
        self.assertEqual(result['phase'], 'running', result)
        self.assertEqual(result['waiting'], {'new-2': 'capacity'}, result)
        self.assertEqual({row['task_id']: row['state'] for row in result['tasks']},
                         {'old-1': 'deferred', 'new-1': 'active', 'new-2': 'pending'})
        self.assertEqual(len(self.api.posts), 2)  # Original prompt plus exactly one new inference request.
        # The retained record stays byte-identical and its worktree/branch/session
        # reservations still prevent reuse, although the worktree advanced externally.
        self.assertEqual(dict(self.dispatcher.task('old-1')), original)
        self.assertEqual(dict(self.dispatcher.db.execute("SELECT * FROM attempt WHERE task_id='old-1'").fetchone()), attempt)
        self.assertEqual(dict(self.dispatcher.db.execute("SELECT * FROM queued WHERE task_id='old-1'").fetchone()), queued)
        self.assertEqual(dict(self.dispatcher.db.execute("SELECT * FROM unit WHERE task_id='old-1'").fetchone()), unit)
        self.assertEqual([tuple(row) for row in self.dispatcher.db.execute(
            "SELECT * FROM observed WHERE task_id='old-1' ORDER BY seq")], observed)
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), self.base)
        with self.assertRaisesRegex(DispatchError, 'reserves'):
            self.approve()
        with self.assertRaisesRegex(DispatchError, 'unresolved'):
            self.dispatcher.submit('new-3', 'worker', 'Change pending to verified.', ['README.md'])
        self.assertEqual(len(self.api.posts), 2)

    def test_deferred_stop_variants_retain_the_inference_reservation(self):
        cases = [
            ('active native session', lambda: self.api.sessions['ses_fixture'].update(active=True), {}),
            ('pending inbox input', lambda: self.api.sessions['ses_fixture']['inbox'].setdefault('msg_held', {'id': 'msg_held'}), {}),
            ('pending form', lambda: self.api.sessions['ses_fixture']['forms'].append({'id': 'form_held'}), {}),
            ('unobserved tail event', lambda: self.api.append('ses_fixture', 'session.metadata.updated'), {}),
            ('non-superseded continuation', lambda: None, {'supersede': False}),
            ('missing continuation', lambda: None, {'prepare': False}),
            ('missing terminal', lambda: self.dispatcher.update('old-1', terminal=None), {}),
            ('unknown delivery', lambda: self.dispatcher.update('old-1', delivered=0), {}),
            ('unresolved observation', lambda: self.dispatcher.update(
                'old-1', issue='Observation incomplete; cursor retained, no request retried'), {}),
            ('foreign input hazard', lambda: self.dispatcher.update(
                'old-1', hazard='Another user input overlaps this assignment'), {}),
        ]
        for label, mutate, stop in cases:
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                self.deferred_capacity(mutate, stop)

    def test_non_deferred_or_missing_queue_records_hold_the_inference_reservation(self):
        # A superseded, proven-idle stopped attempt releases inference only when the
        # coordinator has explicitly deferred the queue record. Every other queue state,
        # and a missing queue record, keep the conservative reservation.
        for label, queue in [('ready', 'ready'), ('active', 'active'), ('pending', 'pending'),
                             ('blocked', 'blocked'), ('continuing', 'continuing'), ('missing', None)]:
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                self.deferred_stop(queue=queue)
                self.assertEqual(self.batch.availability('gpu', self.at), 'capacity')
                self.assertEqual(len(self.api.posts), 1)

    def run_cli(self):
        return subprocess.run([sys.executable, str(Path(__file__).with_name('batch.py')), '--db', str(self.ledger),
                               'run', '--seconds', '0'], capture_output=True, text=True,
                              env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})

    def test_cli_exits_cleanly_without_work_or_when_pool_is_unavailable(self):
        self.assertEqual(self.run_cli().returncode, 0)
        self.configure(state='paused')
        self.enqueue()
        result = self.run_cli()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['phase'], 'paused')
        self.assertEqual(self.api.posts, [])

    def test_cli_stops_on_blocked_admission_without_a_retry(self):
        self.enqueue()
        self.batch.save('doc-1', 'active')
        result = self.run_cli()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)['phase'], 'blocked')
        self.assertEqual(self.api.posts, [])


if __name__ == '__main__':
    unittest.main()
