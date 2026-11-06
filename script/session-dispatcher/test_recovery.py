"""Checkpointed continuations in disposable Git/API fixtures; never use live sessions."""
import json
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
import unittest

from dispatch import Dispatcher, DispatchError, encoded
from recovery import Recovery
import test_batch
import test_lifecycle


class RecoveryTests(unittest.TestCase):
    git = test_batch.BatchTests.git
    approve = test_batch.BatchTests.approve
    commit = test_batch.BatchTests.commit
    enqueue = test_batch.BatchTests.enqueue
    configure = test_batch.BatchTests.configure
    tearDown = test_batch.BatchTests.tearDown

    def setUp(self):
        test_batch.BatchTests.setUp(self)
        self.recovery = Recovery(self.dispatcher)
        self.target = {'providerID': 'fixture', 'id': 'continuation'}

    def started(self, paths=None):
        if paths:
            self.batch.enqueue('doc-1', 'worker', 'Finish the documentation.', paths, self.policy)
        else:
            self.enqueue()
        self.batch.tick()
        self.api.finish(terminal=None)
        self.original = self.dispatcher.task('doc-1')

    def failed(self, quota=False):
        if quota:
            self.api.append('ses_fixture', 'session.step.failed', error={'type': 'provider.free-limit', 'message': 'private details'})
        self.api.append('ses_fixture', 'session.execution.failed')
        self.batch.tick()
        self.assertEqual(self.life.unit('doc-1')['stage'], 'needs_review')

    def prepare(self, checked=False):
        plan = self.recovery.plan('doc-1', self.target)
        self.recovery.prepare('doc-1', self.target, plan['approval_hash'], checked)
        return plan

    def tool(self, terminal=True, executed=True):
        fields = {'id': 'call1', 'assistantMessageID': 'msg_assistant'}
        self.api.append('ses_fixture', 'session.tool.input.started', **fields, name='shell')
        self.api.append('ses_fixture', 'session.tool.called', **fields, executed=executed,
                        input={'command': 'untrusted historical command; never execute'})
        if terminal:
            self.api.append('ses_fixture', 'session.tool.success', **fields, executed=executed)

    def advance_base(self):
        (self.repo/'HANDOVER.md').write_text('Coordinator checkpoint.\n')
        self.git('add', 'HANDOVER.md')
        self.git('commit', '-m', 'Coordinator checkpoint')
        return self.git('rev-parse', 'HEAD')

    def test_explicit_no_tool_baseline_preserves_failed_attempt_and_verifies_delivery(self):
        self.api.sessions['ses_fixture']['info']['model']['providerID'] = 'free'
        self.approve()
        self.batch.configure('free', self.api.connection, 'free', 1, 'available', self.until)
        self.started()
        self.failed(quota=True)
        original_base = json.loads(self.original['registry'])['head']
        base = self.advance_base()
        with self.assertRaises(DispatchError):
            self.prepare()
        plan = self.recovery.plan('doc-1', self.target, base)
        self.assertEqual(plan['baseline_approval']['previous_base'], original_base)
        self.assertEqual(plan['baseline_approval']['changed_paths'], ['HANDOVER.md'])
        self.assertFalse(plan['effects_check_required'])
        self.recovery.prepare('doc-1', self.target, plan['approval_hash'], accept_base=base)
        self.assertEqual(len(self.api.posts), 1)  # Preparation never sends.
        self.configure('paused')
        self.assertEqual(self.batch.tick()['phase'], 'paused')
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(json.loads(self.dispatcher.task('doc-1')['registry'])['head'], original_base)
        self.configure()
        self.batch.tick()
        task = self.dispatcher.task('doc-1')
        self.assertEqual(task['id'], self.original['id'])
        self.assertEqual(task['request_hash'], self.original['request_hash'])
        self.assertEqual(json.loads(task['registry'])['head'], base)
        self.assertEqual(json.loads(self.dispatcher.db.execute('SELECT previous FROM attempt').fetchone()[0])
                         ['task']['registry'], self.original['registry'])
        self.commit()
        self.api.finish()
        self.batch.tick()
        task = self.dispatcher.status('doc-1')
        self.assertEqual(task['state'], 'verified')
        self.assertEqual(task['verification']['base'], base)
        self.assertEqual(task['attempts'][0]['baseline_approval']['accepted_base'], base)

    def test_refresh_supersedes_unsent_blocked_checkpoint_without_overwriting_it(self):
        self.started()
        self.failed()
        old = self.prepare()
        before = dict(self.dispatcher.db.execute('SELECT * FROM attempt').fetchone())
        base = self.advance_base()
        self.assertEqual(self.batch.tick()['phase'], 'blocked')
        self.assertEqual(self.dispatcher.db.execute('SELECT state FROM attempt').fetchone()[0], 'checkpoint_blocked')
        plan = self.recovery.plan('doc-1', self.target, base)
        self.recovery.prepare('doc-1', self.target, plan['approval_hash'], accept_base=base)
        rows = self.dispatcher.db.execute('SELECT * FROM attempt ORDER BY number').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['state'], 'superseded')
        self.assertEqual(rows[0]['plan'], before['plan'])
        self.assertEqual(rows[0]['previous'], before['previous'])
        self.assertEqual(json.loads(rows[0]['plan'])['approval_hash'], old['approval_hash'])
        self.assertNotEqual(json.loads(rows[1]['plan'])['message_id'], old['message_id'])
        for _ in range(2):
            self.recovery.prepare('doc-1', self.target, plan['approval_hash'], accept_base=base)
        self.batch.tick()
        self.batch.tick()
        self.assertEqual(len(self.api.posts), 2)

    def test_baseline_acceptance_refuses_tool_events_even_if_not_executed(self):
        self.started()
        self.tool(executed=False)
        self.failed()
        base = self.advance_base()
        with self.assertRaisesRegex(DispatchError, 'zero recorded tool'):
            self.recovery.plan('doc-1', self.target, base)
        self.assertEqual(len(self.api.posts), 1)

    def test_baseline_acceptance_refuses_unknown_tool_events(self):
        self.started()
        self.api.append('ses_fixture', 'session.tool.input.delta', id='partial')
        self.failed()
        with self.assertRaisesRegex(DispatchError, 'zero recorded tool'):
            self.recovery.plan('doc-1', self.target, self.advance_base())

    def test_baseline_acceptance_refuses_nonforward_history(self):
        self.started()
        self.failed()
        # Only this disposable fixture ref is replaced to model unrelated history.
        root = self.git('commit-tree', 'HEAD^{tree}', '-m', 'Unrelated fixture root')
        self.git('update-ref', 'HEAD', root)
        with self.assertRaises(DispatchError):
            self.recovery.plan('doc-1', self.target, root)
        self.assertEqual(len(self.api.posts), 1)

    def test_baseline_approval_cannot_be_reused_for_a_different_base(self):
        self.started()
        self.failed()
        base = self.advance_base()
        plan = self.recovery.plan('doc-1', self.target, base)
        self.recovery.prepare('doc-1', self.target, plan['approval_hash'], accept_base=base)
        with self.assertRaisesRegex(DispatchError, 'another accepted baseline'):
            self.recovery.prepare('doc-1', self.target, plan['approval_hash'])

    def test_baseline_acceptance_refuses_dirty_or_assigned_output_changes(self):
        self.started()
        self.failed()
        base = self.advance_base()
        (self.repo/'new.txt').write_text('Unattributed partial work.\n')
        with self.assertRaisesRegex(DispatchError, 'exact clean'):
            self.recovery.plan('doc-1', self.target, base)
        (self.repo/'new.txt').unlink()
        (self.repo/'README.md').write_text('Unrelated upstream output.\n')
        self.git('add', 'README.md')
        self.git('commit', '-m', 'Upstream output change')
        with self.assertRaisesRegex(DispatchError, 'assigned output'):
            self.recovery.plan('doc-1', self.target, self.git('rev-parse', 'HEAD'))

    def test_baseline_acceptance_is_immutable_and_not_a_history_rewrite(self):
        self.started()
        self.failed()
        base = self.advance_base()
        with self.assertRaisesRegex(DispatchError, 'immutable'):
            self.recovery.plan('doc-1', self.target, 'HEAD')
        plan = self.recovery.plan('doc-1', self.target, base)
        (self.repo/'HANDOVER.md').write_text('Another coordinator checkpoint.\n')
        self.git('add', 'HANDOVER.md')
        self.git('commit', '-m', 'Later coordinator checkpoint')
        with self.assertRaisesRegex(DispatchError, 'exact clean'):
            self.recovery.prepare('doc-1', self.target, plan['approval_hash'], accept_base=base)
        new_base = self.git('rev-parse', 'HEAD')
        plan = self.recovery.plan('doc-1', self.target, new_base)
        self.recovery.prepare('doc-1', self.target, plan['approval_hash'], accept_base=new_base)
        (self.repo/'HANDOVER.md').write_text('Changed after approval.\n')
        self.assertEqual(self.batch.tick()['phase'], 'blocked')
        self.assertEqual(len(self.api.posts), 1)

    def test_same_task_new_message_and_original_submission_stays_idempotent(self):
        self.started()
        self.failed()
        plan = self.prepare()
        self.assertFalse(plan['effects_check_required'])
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.recovery.prepare('doc-1', self.target, plan['approval_hash'])['state'], 'waiting')
        self.batch.tick()
        task = self.dispatcher.task('doc-1')
        self.assertEqual(task['id'], self.original['id'])
        self.assertNotEqual(task['message_id'], self.original['message_id'])
        self.assertEqual(task['request_hash'], self.original['request_hash'])
        self.assertNotEqual(json.loads(task['payload'])['text'], json.loads(self.original['payload'])['text'])
        self.dispatcher.submit('doc-1', 'worker', 'Change pending to verified.', ['README.md'], learning=self.policy)
        self.batch.tick()
        self.assertEqual(len(self.api.posts), 2)
        row = self.dispatcher.db.execute('SELECT previous FROM attempt').fetchone()
        self.assertEqual(json.loads(row[0])['task']['message_id'], self.original['message_id'])

    def test_quota_handoff_preserves_session_and_does_not_pause_target_pool(self):
        self.api.sessions['ses_fixture']['info']['model']['providerID'] = 'free'
        self.approve()
        self.batch.configure('free', self.api.connection, 'free', 1, 'available', self.until)
        self.started()
        self.failed(quota=True)
        self.assertEqual(self.batch.pool('free')['state'], 'quota')
        self.prepare()
        self.batch.tick()
        task = self.dispatcher.task('doc-1')
        self.assertEqual(task['session_id'], self.original['session_id'])
        self.assertEqual(json.loads(task['registry'])['model'], self.target)
        self.assertEqual(self.batch.pool('gpu')['state'], 'available')
        self.assertEqual(self.batch.pool('free')['state'], 'quota')
        self.commit()
        self.api.finish()
        self.batch.tick()
        unit = self.life.unit('doc-1')
        self.assertEqual(unit['stage'], 'learning', unit)
        self.api.finish(unit['fork_id'])
        self.assertEqual(self.batch.tick()['phase'], 'complete')
        result = self.life.result('doc-1')
        self.assertEqual(result['task']['attempts'][0]['previous_model']['providerID'], 'free')
        self.assertEqual(result['task']['attempts'][0]['previous_terminal'], 'failed')
        self.assertEqual(result['task']['verification']['base'], json.loads(self.original['registry'])['head'])

    def test_waits_for_target_availability_before_switching_or_dispatching(self):
        self.started()
        self.failed()
        self.configure(state='paused')
        self.prepare()
        self.assertEqual(self.batch.tick()['phase'], 'paused')
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.api.sessions['ses_fixture']['info']['model']['id'], 'fixture')
        self.configure()
        self.batch.tick()
        self.assertEqual(len(self.api.posts), 2)

    def test_waiting_continuation_obeys_the_batch_clock_and_window(self):
        self.started()
        self.failed()
        self.prepare()
        self.assertEqual(self.batch.tick(self.at + timedelta(hours=2))['phase'], 'paused')
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.api.sessions['ses_fixture']['info']['model']['id'], 'fixture')

    def test_same_model_can_reuse_its_checked_idle_inference_reservation(self):
        self.started()
        self.failed()
        self.target = json.loads(self.dispatcher.task('doc-1')['registry'])['model']
        self.prepare()
        self.batch.tick()
        self.assertEqual(len(self.api.posts), 2)
        self.assertFalse(self.dispatcher.db.execute("SELECT 1 FROM observed WHERE type='session.model.selected'").fetchone())

    def test_scoped_dirty_staged_and_untracked_work_requires_check_and_is_preserved(self):
        self.started(['README.md', 'new.txt'])
        (self.repo/'README.md').write_text('Partial correction.\n')
        (self.repo/'new.txt').write_text('Retained evidence.\n')
        self.git('add', 'README.md')
        self.tool()
        self.failed()
        with self.assertRaisesRegex(DispatchError, 'effects-checked'):
            self.prepare()
        plan = self.prepare(True)
        before = self.git('diff', '--cached', '--binary')
        self.batch.tick()
        self.assertEqual(self.git('diff', '--cached', '--binary'), before)
        self.assertEqual((self.repo/'new.txt').read_text(), 'Retained evidence.\n')
        self.assertEqual((self.repo/'README.md').read_text(), 'Partial correction.\n')
        self.assertEqual(self.dispatcher.status('doc-1')['attempts'][0]['approval_hash'], plan['approval_hash'])

    def test_commit_before_failure_is_not_recreated(self):
        self.started()
        self.commit()
        commit = self.git('rev-parse', 'HEAD')
        self.failed()
        self.prepare(True)
        self.batch.tick()
        self.api.finish()
        self.batch.tick()
        self.assertEqual(self.git('rev-parse', 'HEAD'), commit)
        self.assertEqual(self.dispatcher.task('doc-1')['state'], 'verified')
        self.assertEqual(self.life.unit('doc-1')['stage'], 'learning')

    def test_preserves_and_verifies_multiple_linear_task_commits(self):
        self.started()
        self.commit()
        first = self.git('rev-parse', 'HEAD')
        self.failed()
        self.prepare(True)
        self.batch.tick()
        (self.repo/'README.md').write_text('Task fully verified.\n')
        self.git('add', 'README.md')
        self.git('commit', '-m', 'docs: finish task\n\nOpenCode-Task: doc-1')
        last = self.git('rev-parse', 'HEAD')
        self.api.finish()
        self.batch.tick()
        proof = json.loads(self.dispatcher.task('doc-1')['verification'])
        self.assertEqual(proof['commits'], [first, last])
        self.assertEqual(self.git('show', '-s', '--format=%P', last), first)

    def test_unfinished_tools_cannot_be_overridden_with_effect_acknowledgement(self):
        self.started()
        self.tool(terminal=False)
        self.failed()
        with self.assertRaisesRegex(DispatchError, 'Unfinished'):
            self.prepare(True)
        self.assertEqual(len(self.api.posts), 1)

    def test_missing_terminal_and_foreign_input_cannot_continue(self):
        self.started()
        with self.assertRaisesRegex(DispatchError, 'attributable stopped'):
            self.prepare(True)
        self.failed()
        self.api.append('ses_fixture', 'session.inbox.enqueued', inboxID='msg_foreign', item={'type': 'user'})
        with self.assertRaisesRegex(DispatchError, 'attributable stopped'):
            self.prepare(True)

    def test_waiting_checkpoint_drift_blocks_before_model_change(self):
        self.started()
        (self.repo/'README.md').write_text('First partial version.\n')
        self.failed()
        self.prepare(True)
        (self.repo/'README.md').write_text('Different partial version.\n')  # Same porcelain status, different bytes.
        self.assertEqual(self.batch.tick()['phase'], 'blocked')
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.api.sessions['ses_fixture']['info']['model']['id'], 'fixture')

    def test_out_of_scope_then_reverted_commit_is_still_rejected(self):
        self.started()
        (self.repo/'outside.txt').write_text('Unrelated work.\n')
        self.git('add', 'outside.txt')
        self.git('commit', '-m', 'outside\n\nOpenCode-Task: doc-1')
        self.git('rm', 'outside.txt')
        self.git('commit', '-m', 'revert outside\n\nOpenCode-Task: doc-1')
        self.failed()
        with self.assertRaisesRegex(DispatchError, 'outside the assignment'):
            self.prepare(True)

    def test_staged_out_of_scope_change_cannot_hide_behind_worktree_restoration(self):
        self.started()
        original = (self.repo/'AGENTS.md').read_text()
        (self.repo/'AGENTS.md').write_text('Out-of-scope staged work.\n')
        self.git('add', 'AGENTS.md')
        (self.repo/'AGENTS.md').write_text(original)
        self.assertEqual(self.git('diff', '--name-only', 'HEAD'), '')
        self.failed()
        with self.assertRaisesRegex(DispatchError, 'outside the assignment'):
            self.prepare(True)
        self.assertIn('Out-of-scope staged work', self.git('show', ':AGENTS.md'))

    def test_lost_model_response_never_repeats_switch_or_prompt(self):
        class LostModel(test_lifecycle.ForkClient):
            def json(inner, method, path, payload=None):
                value = super().json(method, path, payload)
                if method == 'POST' and path.endswith('/model'):
                    self.model_posts += 1
                    raise DispatchError('Model response lost')
                return value
        self.started()
        self.failed()
        self.prepare()
        self.model_posts = 0
        self.dispatcher.client_factory = lambda _: LostModel(self.api)
        self.assertEqual(self.batch.tick()['phase'], 'blocked')
        self.batch.tick()
        self.assertEqual(self.model_posts, 1)
        self.assertEqual(len(self.api.posts), 1)

    def test_native_default_variant_is_canonicalized_without_relaxing_drift(self):
        class DefaultVariant(test_lifecycle.ForkClient):
            def json(inner, method, path, payload=None):
                result = super().json(method, path, payload)
                if method == 'POST' and path.endswith('/model'):
                    info = self.api.sessions['ses_fixture']['info']
                    info['model'] = {**info['model'], 'variant': 'default'}
                return result
        self.started()
        self.failed()
        self.prepare()
        self.dispatcher.client_factory = lambda _: DefaultVariant(self.api)
        self.batch.tick()
        self.assertEqual(len(self.api.posts), 2)
        self.assertEqual(json.loads(self.dispatcher.task('doc-1')['registry'])['model']['variant'], 'default')
        self.api.sessions['ses_fixture']['info']['model']['variant'] = 'high'
        self.assertEqual(self.batch.tick()['phase'], 'blocked')

    def test_unexpected_variant_is_rejected_before_continuation_prompt(self):
        class WrongVariant(test_lifecycle.ForkClient):
            def json(inner, method, path, payload=None):
                result = super().json(method, path, payload)
                if method == 'POST' and path.endswith('/model'):
                    info = self.api.sessions['ses_fixture']['info']
                    info['model'] = {**info['model'], 'variant': 'high'}
                return result
        self.started()
        self.failed()
        self.prepare()
        self.dispatcher.client_factory = lambda _: WrongVariant(self.api)
        self.assertEqual(self.batch.tick()['phase'], 'blocked')
        self.assertEqual(len(self.api.posts), 1)

    def test_lost_continuation_receipt_recovers_without_second_post(self):
        self.started()
        self.failed()
        self.prepare()
        self.api.lose_receipt = True
        self.batch.tick()
        self.batch.tick()
        self.assertEqual(len(self.api.posts), 2)
        self.assertEqual(self.dispatcher.task('doc-1')['enqueued'], 1)

    def test_sending_intent_without_admission_is_not_retried(self):
        self.started()
        self.failed()
        self.prepare()
        self.recovery.save('doc-1', 1, 'sending')
        self.assertEqual(self.batch.tick()['phase'], 'blocked')
        self.assertEqual(len(self.api.posts), 1)

    def test_sending_intent_with_actual_admission_recovers_by_observation(self):
        self.started()
        self.failed()
        self.prepare()
        self.batch.tick()
        self.recovery.save('doc-1', 1, 'sending')
        self.batch.save('doc-1', 'continuing')
        self.batch.tick()
        self.assertEqual(len(self.api.posts), 2)
        self.assertEqual(self.dispatcher.task('doc-1')['state'], 'admitted')

    def test_second_continuation_keeps_both_failed_attempts(self):
        self.started()
        self.failed()
        self.prepare()
        self.batch.tick()
        self.api.finish(terminal='failed')
        self.batch.tick()
        plan = self.prepare()
        self.assertEqual(plan['number'], 2)
        self.batch.tick()
        history = self.dispatcher.status('doc-1')['attempts']
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]['previous_message_id'], history[0]['message_id'])
        self.assertEqual(len({payload['id'] for _,payload in self.api.posts}), 3)

    def test_completed_learning_unit_cannot_be_restarted_as_task(self):
        self.started()
        self.commit()
        self.api.append('ses_fixture', 'session.execution.succeeded')
        self.batch.tick()
        with self.assertRaisesRegex(DispatchError, 'learning stage'):
            self.prepare(True)

    def test_net_empty_history_is_not_called_a_delivery(self):
        self.started()
        self.commit()
        self.failed()
        self.prepare(True)
        self.batch.tick()
        (self.repo/'README.md').write_text('Task status: pending.\n')
        self.git('add', 'README.md')
        self.git('commit', '-m', 'revert result\n\nOpenCode-Task: doc-1')
        self.api.finish()
        self.assertEqual(self.batch.tick()['phase'], 'blocked')
        self.assertEqual(self.api.forks, 0)

    def test_bytes_changed_during_model_handoff_are_detected(self):
        class ConcurrentEdit(test_lifecycle.ForkClient):
            def json(inner, method, path, payload=None):
                result = super().json(method, path, payload)
                if method == 'POST' and path.endswith('/model'):
                    (self.repo/'README.md').write_text('Later partial version.\n')
                return result
        self.started()
        (self.repo/'README.md').write_text('Approved partial version.\n')
        self.failed()
        self.prepare(True)
        self.dispatcher.client_factory = lambda _: ConcurrentEdit(self.api)
        self.assertEqual(self.batch.tick()['phase'], 'blocked')
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual((self.repo/'README.md').read_text(), 'Later partial version.\n')

    def test_cli_plan_and_preparation_are_idempotent_without_dispatch(self):
        self.started()
        self.failed()
        command = [sys.executable, str(Path(__file__).with_name('recovery.py')), '--db', str(self.ledger)]
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        planned = subprocess.run(command + ['plan', 'doc-1', '--model', encoded(self.target)], env=env, capture_output=True, text=True)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        approval = json.loads(planned.stdout)['approval_hash']
        for _ in range(2):
            prepared = subprocess.run(command + ['prepare', 'doc-1', '--model', encoded(self.target), '--approve', approval],
                                      env=env, capture_output=True, text=True)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            self.assertEqual(json.loads(prepared.stdout)['state'], 'waiting')
        self.assertEqual(len(self.api.posts), 1)

    def test_status_does_not_opt_a_legacy_ledger_into_recovery(self):
        legacy = Dispatcher(self.root/'legacy.sqlite3')
        try:
            legacy.approve('legacy', 'ses_fixture', self.repo, self.api.connection)
            legacy.submit('legacy-1', 'legacy', 'Update README.', ['README.md'])
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('recovery.py')), '--db', str(legacy.path),
                                     'status', 'legacy-1'], capture_output=True, text=True,
                                    env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(legacy.db.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertFalse(legacy.db.execute("SELECT 1 FROM sqlite_master WHERE name='attempt'").fetchone())
        finally:
            legacy.close()


if __name__ == '__main__':
    unittest.main()
