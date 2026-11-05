"""Real disposable Git merges and loopback event fixtures; no live or paid sessions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from dispatch import Dispatcher, DispatchError, encoded
from integrate import Integration
from lifecycle import Lifecycle
import review
import test_lifecycle as fixtures


class IntegrationTests(unittest.TestCase):
    git = fixtures.LifecycleTests.git
    approve = fixtures.LifecycleTests.approve
    commit = fixtures.LifecycleTests.commit
    submit = fixtures.LifecycleTests.submit
    completed = fixtures.LifecycleTests.completed
    learning = fixtures.LifecycleTests.learning
    tearDown = fixtures.LifecycleTests.tearDown

    def setUp(self):
        fixtures.LifecycleTests.setUp(self)
        self.target = self.root / 'target'
        self.git('worktree', 'add', '-b', 'integration-main', str(self.target))
        self.target_base = self.target_git('rev-parse', 'HEAD')
        self.gate = Integration(self.dispatcher)

    def target_git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.target), *args], stderr=subprocess.PIPE).decode().strip()

    def enrolled(self):
        return self.gate.enroll('doc-1', self.target, 'gpt-6.1-sol', [], 'Fixture checks passed; no runtime claims.')['id']

    def frozen(self, *, learning=False):
        if learning:
            unit = self.learning()
            (self.repo / 'AGENTS.md').write_text('Retain exact fixture receipts.\n')
            self.api.finish(unit['fork_id'])
            self.assertEqual(self.life.advance('doc-1')['stage'], 'ready')
        else:
            self.policy['wait_seconds'] = 0
            self.submit()
            self.commit()
            self.api.finish()
        key = self.enrolled()
        self.assertEqual(self.gate.advance(key)['stage'], 'await_review')
        return key

    def reviewed(self, key, verdict=None, risks=(), escalate=False):
        item = self.gate.item(key)
        planned = review.plan(self.dispatcher, 'doc-1', item['request']['model'], list(risks),
            item['request']['evidence'], integration=item['snapshot']['context'])

        def runner(command, **kwargs):
            Path(command[command.index('--output-last-message') + 1]).write_text(encoded(
                verdict or {'assessment': 'routine', 'issues': [], 'escalation': []}))
            return subprocess.CompletedProcess(command, 0)

        with patch.dict(os.environ, {}, clear=True), patch('review.shutil.which', return_value='/fixture/codex'):
            return review.run(self.dispatcher, key, planned, runner=runner, escalate=escalate)

    def test_separate_task_learning_commits_merge_once_and_release_source(self):
        key = self.frozen(learning=True)
        tip = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.enrolled(), key)
        with self.assertRaisesRegex(DispatchError, 'reserves'):
            self.approve()
        self.reviewed(key)
        self.assertEqual(self.gate.advance(key)['stage'], 'await_approval')
        self.assertEqual(self.target_git('rev-parse', 'HEAD'), self.target_base)
        self.gate.approve(key, [])
        result = self.gate.advance(key)
        self.assertEqual(result['stage'], 'merged', result)
        merged = self.target_git('rev-parse', 'HEAD')
        self.assertEqual(self.target_git('show', '-s', '--format=%P', merged).split(), [self.target_base, tip])
        self.assertEqual(self.target_git('rev-list', '--count', self.target_base + '..HEAD'), '3')
        self.assertEqual((self.target / 'AGENTS.md').read_text(), 'Retain exact fixture receipts.\n')
        self.assertEqual(self.gate.advance(key)['receipt']['commit'], merged)
        self.approve()

    def test_observer_reconnect_reuses_review_and_does_not_admit_again(self):
        key = self.frozen()
        self.reviewed(key)
        self.dispatcher.close()
        self.dispatcher = Dispatcher(self.ledger, lambda _: fixtures.ForkClient(self.api))
        self.gate = Integration(self.dispatcher)
        self.assertEqual(self.gate.advance(key)['stage'], 'await_approval')
        self.assertEqual(self.dispatcher.db.execute('SELECT COUNT(*) FROM review').fetchone()[0], 1)
        self.assertEqual(len(self.api.posts), 1)

    def test_enrollment_before_completion_allows_only_its_learning_fork(self):
        self.submit()
        key = self.enrolled()
        self.commit()
        self.api.finish()
        self.assertEqual(self.gate.advance(key, gpu_available=True)['stage'], 'waiting_task')
        unit = self.life.unit('doc-1')
        self.assertEqual(unit['stage'], 'learning')
        self.api.finish(unit['fork_id'])
        self.assertEqual(self.gate.advance(key)['stage'], 'await_review')
        self.assertEqual(self.api.forks, 1)
        with self.assertRaisesRegex(DispatchError, 'reserves'):
            self.approve()

    def test_approval_requires_a_review_of_this_exact_target_context(self):
        key = self.frozen()
        with self.assertRaisesRegex(DispatchError, 'structured review'):
            self.gate.approve(key, [])
        planned = review.plan(self.dispatcher, 'doc-1', 'gpt-6.1-sol', [], 'Different evidence.')
        self.dispatcher.db.execute('CREATE TABLE review (task_id,attempt,input_hash,state,result)')
        self.dispatcher.db.execute('INSERT INTO review VALUES (?,1,?,?,?)',
            (key, planned['input_hash'], 'reviewed', encoded({'plan': planned,
             'assessment': {'assessment': 'routine', 'issues': [], 'escalation': []}})))
        with self.assertRaisesRegex(DispatchError, 'different evidence or unit'):
            self.gate.approve(key, [])
        self.assertEqual(self.target_git('rev-parse', 'HEAD'), self.target_base)

    def test_target_drift_invalidates_approval_without_merging(self):
        key = self.frozen()
        self.reviewed(key)
        self.gate.approve(key, [])
        (self.target / 'extra.md').write_text('Concurrent target work.\n')
        self.target_git('add', 'extra.md')
        self.target_git('commit', '-m', 'independent target work')
        head = self.target_git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(DispatchError, 'advanced'):
            self.gate.advance(key)
        self.assertEqual(self.target_git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.gate.status(key)['stage'], 'approved')

    def test_dirty_source_untracked_artifact_prevents_merge(self):
        key = self.frozen()
        self.reviewed(key)
        self.gate.approve(key, [])
        (self.repo / 'evidence.local').write_text('Keep me.\n')
        with self.assertRaisesRegex(DispatchError, 'dirty'):
            self.gate.advance(key)
        self.assertEqual(self.target_git('rev-parse', 'HEAD'), self.target_base)
        self.assertTrue((self.repo / 'evidence.local').exists())

    def test_explicit_deferral_can_revoke_approval_before_any_merge_intent(self):
        key = self.frozen()
        self.reviewed(key)
        approved = self.gate.approve(key, [])
        deferred = self.gate.defer(key, 'Operator cancelled before merge.')
        self.assertEqual(deferred['stage'], 'deferred')
        self.assertEqual(deferred['approval'], approved['approval'])
        self.assertEqual(self.target_git('rev-parse', 'HEAD'), self.target_base)
        self.approve()

    def test_conflicts_stop_in_preflight_without_touching_target(self):
        (self.target / 'README.md').write_text('Conflicting main edit.\n')
        self.target_git('add', 'README.md')
        self.target_git('commit', '-m', 'independent conflicting edit')
        target = self.target_git('rev-parse', 'HEAD')
        self.policy['wait_seconds'] = 0
        self.submit()
        self.commit()
        self.api.finish()
        key = self.enrolled()
        self.assertEqual(self.gate.advance(key)['stage'], 'blocked')
        self.assertEqual(self.target_git('rev-parse', 'HEAD'), target)
        self.assertEqual(self.target_git('status', '--porcelain'), '')
        self.assertEqual(self.gate.defer(key, 'Resolve separately.')['stage'], 'deferred')
        self.approve()

    def test_review_contains_new_main_changes_and_proposed_merge(self):
        (self.target / 'context.md').write_text('New target context.\n')
        self.target_git('add', 'context.md')
        self.target_git('commit', '-m', 'independent docs')
        key = self.frozen()
        planned = self.gate.item(key)['snapshot']['review']
        self.assertIn('New target context.', planned['prompt'])
        self.assertIn('proposed_target_diff', planned['prompt'])
        self.assertEqual(planned['effort'], 'medium')

    def test_explicit_replan_for_new_target_keeps_both_review_scopes(self):
        key = self.frozen()
        self.reviewed(key)
        (self.target / 'context.md').write_text('New target context.\n')
        self.target_git('add', 'context.md')
        self.target_git('commit', '-m', 'independent docs')
        with self.assertRaisesRegex(DispatchError, 'advanced'):
            self.gate.advance(key)
        self.gate.defer(key, 'Review new target explicitly.')
        new_key = self.enrolled()
        self.assertNotEqual(new_key, key)
        self.assertEqual(self.gate.advance(new_key)['stage'], 'await_review')
        self.reviewed(new_key)
        self.gate.approve(new_key, [])
        self.assertEqual(self.gate.advance(new_key)['stage'], 'merged')
        scopes = {row[0] for row in self.dispatcher.db.execute('SELECT task_id FROM review')}
        self.assertEqual(scopes, {key, new_key})

    def test_review_issues_cannot_be_waived_and_need_explicit_escalation(self):
        key = self.frozen()
        self.reviewed(key, {'assessment': 'routine', 'issues': ['Conflicting evidence.'], 'escalation': []})
        held = self.gate.advance(key, run_review=True)
        self.assertTrue(held['review_hold'])
        with self.assertRaisesRegex(DispatchError, 'escalation'):
            self.gate.approve(key, [])
        self.reviewed(key, risks=['conflicting_evidence'], escalate=True)
        with self.assertRaisesRegex(DispatchError, 'Acknowledge'):
            self.gate.approve(key, [])
        self.gate.approve(key, ['conflicting_evidence'])
        self.assertEqual(self.gate.advance(key)['stage'], 'merged')

    def test_interrupted_merge_after_actual_commit_never_retries(self):
        key = self.frozen()
        self.reviewed(key)
        self.gate.approve(key, [])
        import integrate
        real_identity = integrate.identity

        def lost_result(path):
            if str(path) == str(self.target) and self.gate.item(key)['stage'] == 'merging':
                raise DispatchError('Fixture lost observation after Git committed')
            return real_identity(path)

        with patch('integrate.identity', side_effect=lost_result):
            result = self.gate.advance(key)
        self.assertEqual(result['stage'], 'uncertain')
        merged = self.target_git('rev-parse', 'HEAD')
        self.assertNotEqual(merged, self.target_base)
        self.assertEqual(self.gate.advance(key)['stage'], 'uncertain')
        self.assertEqual(self.target_git('rev-parse', 'HEAD'), merged)
        with self.assertRaisesRegex(DispatchError, 'uncertain'):
            self.gate.defer(key, 'Not safe to release.')

    def test_persisted_merging_intent_is_held_even_if_no_merge_happened(self):
        key = self.frozen()
        self.reviewed(key)
        self.gate.approve(key, [])
        self.gate.save(key, 'merging')
        self.assertEqual(self.gate.advance(key)['stage'], 'uncertain')
        self.assertEqual(self.target_git('rev-parse', 'HEAD'), self.target_base)

    def test_hook_failure_preserves_uncommitted_merge_and_holds_both_worktrees(self):
        key = self.frozen()
        self.reviewed(key)
        self.gate.approve(key, [])
        hook = Path(self.git('rev-parse', '--git-common-dir')) / 'hooks' / 'pre-merge-commit'
        if not hook.is_absolute():
            hook = self.repo / hook
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o700)
        self.assertEqual(self.gate.advance(key)['stage'], 'uncertain')
        self.assertTrue((self.target / 'README.md').read_text().endswith('verified.\n'))
        self.assertEqual(self.target_git('rev-parse', 'HEAD'), self.target_base)
        self.assertTrue(self.target_git('status', '--porcelain'))
        self.assertEqual(self.gate.advance(key)['stage'], 'uncertain')

    def test_custom_merge_driver_is_not_executed_by_preflight(self):
        self.git('config', 'merge.fixture.driver', 'false')
        self.policy['wait_seconds'] = 0
        self.submit()
        self.commit()
        self.api.finish()
        key = self.enrolled()
        result = self.gate.advance(key)
        self.assertEqual(result['stage'], 'blocked')
        self.assertIn('Custom merge drivers', result['receipt']['issue'])
        self.assertEqual(self.target_git('status', '--porcelain'), '')

    def test_already_reachable_source_is_recorded_without_another_commit(self):
        self.policy['wait_seconds'] = 0
        self.submit()
        self.commit()
        self.api.finish()
        tip = self.git('rev-parse', 'HEAD')
        self.target_git('merge', '--ff-only', tip)
        key = self.enrolled()
        self.gate.advance(key)
        self.reviewed(key)
        self.gate.approve(key, [])
        result = self.gate.advance(key)
        self.assertEqual(result['stage'], 'merged')
        self.assertTrue(result['receipt']['already_integrated'])
        self.assertEqual(self.target_git('rev-parse', 'HEAD'), tip)

    def test_approved_target_is_reserved_against_other_dispatch(self):
        key = self.frozen()
        sid = self.api.session(self.target, 'ses_target')
        self.dispatcher.approve('target', sid, self.target, self.api.connection)
        self.reviewed(key)
        self.gate.approve(key, [])
        with self.assertRaisesRegex(DispatchError, 'integration reserves'):
            self.dispatcher.submit('other', 'target', 'Other task', ['README.md'])

    def test_active_target_session_cannot_be_integrated(self):
        key = self.frozen()
        sid = self.api.session(self.target, 'ses_target')
        self.api.sessions[sid]['active'] = True
        with self.assertRaisesRegex(DispatchError, 'active native session'):
            self.gate.advance(key)

    def test_viewed_source_does_not_invalidate_frozen_review(self):
        key = self.frozen()
        self.reviewed(key)
        self.api.append('ses_fixture', 'session.viewed', idle=1)
        self.gate.approve(key, [])
        self.assertEqual(self.gate.advance(key)['stage'], 'merged')

    def test_recovery_and_batch_do_not_downgrade_integration_schema(self):
        from recovery import Recovery
        Recovery(self.dispatcher)
        self.assertEqual(self.dispatcher.db.execute('PRAGMA user_version').fetchone()[0], 5)

    def test_real_cli_watch_stops_at_approval_and_status_does_not_run_inference(self):
        key = self.frozen()
        self.reviewed(key)
        command = [sys.executable, str(Path(__file__).with_name('integrate.py')), '--db', str(self.ledger)]
        result = subprocess.run([*command, 'watch', key, '--seconds', '0'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]['stage'], 'await_approval')
        self.assertEqual(self.target_git('rev-parse', 'HEAD'), self.target_base)

    def test_status_does_not_enable_integration_in_an_old_ledger(self):
        self.dispatcher.db.execute('DROP TABLE integration')
        self.dispatcher.db.execute('PRAGMA user_version=2')
        command = [sys.executable, str(Path(__file__).with_name('integrate.py')), '--db', str(self.ledger)]
        result = subprocess.run([*command, 'status', 'not-enrolled'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('status does not enable', result.stderr)
        self.assertEqual(self.dispatcher.db.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertFalse(self.dispatcher.db.execute("SELECT 1 FROM sqlite_master WHERE name='integration'").fetchone())


if __name__ == '__main__':
    unittest.main()
