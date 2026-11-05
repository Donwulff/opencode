"""Disposable worktrees and API contract fixtures. No real sessions or paid inference."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from dispatch import Client, Dispatcher, DispatchError, digest, encoded
from lifecycle import Lifecycle, snapshot_policy
import review
import test_dispatch as fixtures


class ForkClient:
    def __init__(self, api):
        self.api = api
        self.client = Client(api.connection)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def json(self, method, path, payload=None):
        if method == "POST" and path.endswith("/fork"):
            self.api.forks += 1
            parent = self.api.sessions[path.split('/')[3]]["info"]
            sid = self.api.session(Path(parent["location"]["directory"]), "ses_learning")
            self.api.sessions[sid]["info"]["fork"] = {"sessionID": parent["id"], "boundary": "msg_fixture"}
            if self.api.lose_fork:
                raise DispatchError("Fork response lost; effects unknown")
            return {"data": copy.deepcopy(self.api.sessions[sid]["info"])}
        if method == "PATCH":
            self.api.sessions[path.split('/')[3]]["info"].update(payload)
            return None
        if method == "POST" and path.endswith("/model"):
            sid = path.split('/')[3]
            self.api.sessions[sid]["info"]["model"] = payload["model"]
            self.api.append(sid, "session.model.selected", model=payload["model"])
            return None
        return self.client.json(method, path, payload)


class LifecycleTests(unittest.TestCase):
    git = fixtures.DispatcherTests.git
    approve = fixtures.DispatcherTests.approve
    commit = fixtures.DispatcherTests.commit
    tearDown = fixtures.DispatcherTests.tearDown

    def setUp(self):
        fixtures.DispatcherTests.setUp(self)
        (self.repo / 'AGENTS.md').write_text('Preserve the fixture.\n')
        self.git('add', 'AGENTS.md')
        self.git('commit', '-m', 'fixture instructions')
        self.approve()
        prompt = self.root / 'learn.md'
        prompt.write_text('---\ndescription: Learn\n---\nUse only edit; no commands. Zero edits is valid. $ARGUMENTS\n')
        self.policy = {'prompt': str(prompt), 'model': {'providerID': 'fixture', 'id': 'learner'}, 'allow': ['AGENTS.md']}
        self.api.forks = 0
        self.api.lose_fork = False
        self.dispatcher.client_factory = lambda _: ForkClient(self.api)
        self.life = Lifecycle(self.dispatcher)

    def submit(self, task='doc-1'):
        return self.dispatcher.submit(task, 'worker', 'Change pending to verified.', ['README.md'], learning=self.policy)

    def completed(self):
        self.submit()
        self.commit()
        self.api.finish()
        unit = self.life.advance('doc-1')
        self.assertEqual(unit['stage'], 'await_gpu')
        return unit

    def learning(self):
        self.completed()
        unit = self.life.advance('doc-1', True)
        self.assertEqual(unit['stage'], 'learning', unit)
        return unit

    def test_task_verification_does_not_release_worktree(self):
        self.completed()
        with self.assertRaisesRegex(DispatchError, 'reserves'):
            self.approve()
        self.assertEqual(self.api.forks, 0)

    def test_no_op_is_accounted_and_releases_without_recursive_learning(self):
        unit = self.learning()
        parent = copy.deepcopy(self.api.sessions['ses_fixture']['info'])
        self.api.finish(unit['fork_id'])
        result = self.life.advance('doc-1', True)
        self.assertEqual(result['stage'], 'ready', result)
        self.assertEqual(result['detail']['learning'], 'completed_no_changes')
        self.assertIsNone(result['detail']['learning_commit'])
        self.assertEqual(self.dispatcher.task(unit['child_id'])['state'], 'accounted')
        self.assertEqual(self.life.advance('doc-1', True)['stage'], 'ready')
        self.assertEqual(self.api.forks, 1)
        self.assertEqual(self.api.sessions['ses_fixture']['info'], parent)
        self.assertEqual(self.api.sessions[unit['fork_id']]['info']['model'], self.policy['model'])
        self.assertNotIn('command', json.loads(self.dispatcher.task(unit['child_id'])['payload']))
        self.assertEqual(json.loads(self.dispatcher.task(unit['child_id'])['payload'])['metadata']['template_hash'],
                         digest([unit['config']['wrapper_hash'], unit['config']['prompt_hash']]))
        self.assertTrue(self.life.result('doc-1')['human_integration_approval_required'])
        self.approve()

    def test_learning_commit_is_separate_and_scoped(self):
        unit = self.learning()
        original = self.git('rev-parse', 'HEAD')
        (self.repo / 'AGENTS.md').write_text('Preserve the fixture. Use exact receipt IDs.\n')
        self.api.finish(unit['fork_id'])
        result = self.life.advance('doc-1')
        self.assertEqual(result['stage'], 'ready', result)
        commit = result['detail']['learning_commit']
        self.assertEqual(self.git('show', '-s', '--format=%P', commit), original)
        self.assertEqual(self.git('diff', '--name-only', original, commit), 'AGENTS.md')
        self.assertEqual(self.life.result('doc-1')['task']['verification']['commit'], original)

    def test_quota_failure_preserves_task_and_partial_learning_proposal(self):
        unit = self.learning()
        (self.repo / 'AGENTS.md').write_text('Partial proposal; review it.\n')
        self.api.append(unit['fork_id'], 'session.step.failed', error={'message': 'quota exhausted'})
        self.api.finish(unit['fork_id'], 'failed')
        result = self.life.advance('doc-1')
        self.assertEqual(result['stage'], 'ready', result)
        self.assertEqual(result['detail']['learning'], 'failed')
        self.assertIsNotNone(result['detail']['learning_commit'])
        self.assertEqual(self.dispatcher.task('doc-1')['state'], 'verified')
        self.assertEqual([row['category'] for row in self.life.result('doc-1')['feedback']], ['quota'])

    def test_failed_no_change_is_not_reported_as_success(self):
        unit = self.learning()
        self.api.finish(unit['fork_id'], 'failed')
        result = self.life.advance('doc-1')
        self.assertEqual(result['detail']['learning'], 'failed_no_changes')
        self.assertEqual(result['stage'], 'ready')

    def test_non_edit_writer_is_accounted_as_failed_policy_not_success(self):
        unit = self.learning()
        self.api.append(unit['fork_id'], 'session.tool.input.started', id='tool_writer', name='write')
        self.api.append(unit['fork_id'], 'session.tool.success', id='tool_writer', executed=True, content=[{'type': 'text', 'text': 'fixture'}])
        (self.repo / 'AGENTS.md').write_text('Proposal made with disallowed writer.\n')
        self.api.finish(unit['fork_id'])
        result = self.life.advance('doc-1')
        self.assertEqual(result['stage'], 'ready', result)
        self.assertEqual(result['detail']['learning'], 'failed_tool_policy')
        self.assertEqual([row['category'] for row in self.life.result('doc-1')['feedback']], ['non_edit_writer_succeeded'])

    def test_native_guard_and_edit_feedback_are_deduplicated(self):
        unit = self.learning()
        self.api.append(unit['fork_id'], 'session.tool.failed', error={'message': 'The /learn command may only modify AGENTS.md files.'})
        self.api.append(unit['fork_id'], 'session.tool.failed', error={'message': 'Could not find oldString in AGENTS.md.'})
        self.api.finish(unit['fork_id'])
        self.life.advance('doc-1')
        self.life.advance('doc-1')
        rows = self.life.result('doc-1')['feedback']
        self.assertEqual(sorted(row['category'] for row in rows), ['command_guard', 'edit_match_failed'])
        self.assertTrue(all(row['event_id'].startswith('evt_') and row['template_hash'] != 'legacy' for row in rows))

    def test_lost_fork_response_is_never_repeated(self):
        self.completed()
        self.api.lose_fork = True
        result = self.life.advance('doc-1', True)
        self.assertEqual(result['stage'], 'needs_review')
        self.life.advance('doc-1', True)
        self.assertEqual(self.api.forks, 1)
        with self.assertRaises(DispatchError):
            self.approve()

    def test_interrupted_observation_recovers_without_second_prompt(self):
        unit = self.learning()
        self.dispatcher.close()
        self.api.finish(unit['fork_id'])
        self.dispatcher = Dispatcher(self.ledger, lambda _: ForkClient(self.api))
        self.life = Lifecycle(self.dispatcher)
        self.assertEqual(self.life.advance('doc-1')['stage'], 'ready')
        self.assertEqual(len(self.api.posts), 2)

    def test_unexpected_edit_or_foreign_message_holds_worktree(self):
        unit = self.learning()
        (self.repo / 'README.md').write_text('unexpected learning edit\n')
        self.api.finish(unit['fork_id'])
        result = self.life.advance('doc-1')
        self.assertEqual(result['stage'], 'needs_review')
        self.assertIn('outside', result['detail']['issue'])

    def test_original_session_continuation_is_detected(self):
        self.completed()
        self.api.append('ses_fixture', 'session.execution.started')
        self.assertEqual(self.life.advance('doc-1', True)['stage'], 'needs_review')
        self.assertEqual(self.api.forks, 0)

    def test_interrupted_commit_is_not_repeated(self):
        unit = self.learning()
        (self.repo / 'AGENTS.md').write_text('Scoped learning proposal.\n')
        self.api.finish(unit['fork_id'])
        from dispatch import git as real_git

        def crash_after_commit(path, *args):
            value = real_git(path, *args)
            if args[0] == 'commit':
                raise DispatchError('Fixture: commit happened but observer lost its result')
            return value

        with patch('lifecycle.git', side_effect=crash_after_commit):
            result = self.life.advance('doc-1')
        self.assertEqual(result['stage'], 'needs_review')
        committed = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(committed, unit['detail']['task_commit'])
        self.life.advance('doc-1')
        self.assertEqual(self.git('rev-parse', 'HEAD'), committed)
        with self.assertRaises(DispatchError):
            self.approve()

    def test_two_watchers_cannot_commit_concurrently(self):
        import fcntl
        self.completed()
        with self.ledger.open('rb') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.life.advance('doc-1', True)['stage'], 'await_gpu')
        self.assertEqual(self.api.forks, 0)

    def test_gpu_gate_capacity_and_bounded_wait(self):
        self.policy['wait_seconds'] = 0
        self.submit()
        self.commit()
        self.api.finish()
        result = self.life.advance('doc-1')
        self.assertEqual(result['stage'], 'ready')
        self.assertEqual(result['detail']['learning'], 'skipped_gpu_unavailable')
        self.assertEqual(self.api.forks, 0)

    def test_capacity_counts_existing_provider_tasks(self):
        self.policy['max_active'] = 1
        self.completed()
        self.api.session(self.repo, 'ses_other')
        self.api.sessions['ses_other']['active'] = True
        self.assertEqual(self.life.advance('doc-1', True)['stage'], 'await_gpu')
        self.assertEqual(self.api.forks, 0)

    def test_snapshot_is_immutable_and_duplicate_submit_does_not_send(self):
        self.submit()
        Path(self.policy['prompt']).write_text('New unreviewed prompt\n')
        self.submit()
        self.assertEqual(len(self.api.posts), 1)
        self.assertNotIn('New unreviewed', self.life.unit('doc-1')['config']['prompt'])

    def test_real_cli_policy_submission_and_bounded_watch(self):
        policy = self.root / 'policy.json'
        policy.write_text(encoded(self.policy))
        assignment = self.root / 'assignment.txt'
        assignment.write_text('Change pending to verified.')
        command = [sys.executable, str(Path(__file__).with_name('dispatch.py')), '--db', str(self.ledger),
                   'submit', 'doc-1', '--session', 'worker', '--task-file', str(assignment), '--allow', 'README.md',
                   '--learning-policy', str(policy)]
        admitted = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(admitted.returncode, 0, admitted.stderr)
        self.assertEqual(json.loads(admitted.stdout)['state'], 'admitted')
        self.commit()
        self.api.finish()
        watched = subprocess.run([sys.executable, str(Path(__file__).with_name('lifecycle.py')), '--db', str(self.ledger),
                                  'watch', 'doc-1', '--seconds', '0'], capture_output=True, text=True)
        self.assertEqual(watched.returncode, 2, watched.stderr)
        self.assertEqual(json.loads(watched.stdout)[0]['stage'], 'await_gpu')
        self.assertEqual(len(self.api.posts), 1)

    def test_learning_policy_scope_and_concurrency_rejected(self):
        for change in ({'allow': ['README.md']}, {'max_active': 4}):
            with self.assertRaises(DispatchError):
                snapshot_policy({**self.policy, **change}, self.repo)

    def test_non_enrolled_tasks_are_not_adopted(self):
        with self.assertRaises(DispatchError):
            self.life.advance('not-enrolled')

    def test_v1_migration_preserves_registry_and_assignment(self):
        self.dispatcher.submit('legacy', 'worker', 'Change pending to verified.', ['README.md'])
        saved = self.dispatcher.task('legacy')
        self.dispatcher.db.execute('DROP TABLE unit')
        self.dispatcher.db.execute('DROP TABLE feedback')
        self.dispatcher.db.execute('PRAGMA user_version=1')
        self.dispatcher.close()
        self.dispatcher = Dispatcher(self.ledger, lambda _: ForkClient(self.api))
        self.life = Lifecycle(self.dispatcher)
        self.assertEqual(self.dispatcher.task('legacy'), saved)
        self.assertEqual(self.dispatcher.db.execute('PRAGMA user_version').fetchone()[0], 2)
        with self.assertRaisesRegex(DispatchError, 'unresolved'):
            self.dispatcher.submit('another', 'worker', 'Different task.', ['README.md'])

    def ready_review(self):
        unit = self.learning()
        self.api.finish(unit['fork_id'])
        self.life.advance('doc-1')
        return review.plan(self.dispatcher, 'doc-1', 'gpt-6.1-sol', [], 'Fixture tests passed; no runtime claims.')

    def test_review_effort_and_explicit_escalation(self):
        result = self.ready_review()
        self.assertEqual(result['effort'], 'medium')
        self.assertEqual(review.plan(self.dispatcher, 'doc-1', 'gpt-6.1-sol', ['failed_tests'], 'failed')['effort'], 'high')
        self.assertEqual(review.plan(self.dispatcher, 'doc-1', 'gpt-6.1-sol', ['memory_safety'], 'unclear')['effort'], 'xhigh')

    def test_view_acknowledgements_do_not_change_task_proof_or_review(self):
        planned = self.ready_review()
        task = self.dispatcher.task('doc-1')
        event = self.api.append('ses_fixture', 'session.viewed', idle=1)
        result = self.life.result('doc-1')
        self.assertEqual(result['post_verification_views']['views'][0]['id'], event['id'])
        self.assertEqual(self.dispatcher.task('doc-1'), task)
        self.assertEqual(review.plan(self.dispatcher, 'doc-1', 'gpt-6.1-sol', [],
            'Fixture tests passed; no runtime claims.'), planned)

    def test_views_during_learning_are_accounted_without_reopening_task(self):
        unit = self.learning()
        self.api.append('ses_fixture', 'session.viewed', idle=1)
        self.api.finish(unit['fork_id'])
        self.assertEqual(self.life.advance('doc-1')['stage'], 'ready')

    def test_non_view_suffix_still_holds_a_completed_unit(self):
        self.ready_review()
        for kind in ('session.execution.started', 'session.inbox.enqueued', 'session.model.selected', 'session.metadata.updated'):
            with self.subTest(kind=kind):
                self.api.append('ses_fixture', kind)
                with self.assertRaisesRegex(DispatchError, 'advanced'):
                    self.life.result('doc-1')
                self.api.sessions['ses_fixture']['events'].pop()

    def test_view_suffix_requires_contiguous_events_and_watermark(self):
        self.ready_review()
        first = self.api.append('ses_fixture', 'session.viewed', idle=1)
        self.api.append('ses_fixture', 'session.viewed', idle=1)
        self.api.sessions['ses_fixture']['events'].remove(first)
        with self.assertRaisesRegex(DispatchError, 'gap'):
            self.life.result('doc-1')
        self.api.sessions['ses_fixture']['events'].insert(first['durable']['seq'], first)
        self.api.retain = False
        with self.assertRaisesRegex(DispatchError, 'incomplete'):
            self.life.result('doc-1')

    def test_native_paths_remain_visible_without_equating_file_type_with_ambiguity(self):
        self.policy['wait_seconds'] = 0
        self.dispatcher.submit('native', 'worker', 'A fixture header.', ['example.h'], learning=self.policy)
        (self.repo / 'example.h').write_text('/* fixture */\n')
        self.git('add', 'example.h')
        self.git('commit', '-m', 'fixture source\n\nOpenCode-Task: native')
        self.api.finish()
        self.assertEqual(self.life.advance('native')['stage'], 'ready')
        planned = review.plan(self.dispatcher, 'native', 'gpt-6.1-sol', [], 'Fixture only.')
        self.assertIn('example.h', planned['prompt'])
        self.assertEqual(planned['effort'], 'medium')
        self.assertEqual(review.plan(self.dispatcher, 'native', 'gpt-6.1-sol',
            ['source_ambiguity'], 'Fixture only.')['effort'], 'xhigh')

    def test_review_refuses_nested_codex(self):
        result = self.ready_review()
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'parent'}):
            with self.assertRaisesRegex(DispatchError, 'nested'):
                review.run(self.dispatcher, 'doc-1', result)

    def test_one_structured_review_no_shell_or_delegation(self):
        result = self.ready_review()

        def run(command, **kwargs):
            self.assertIn('model_reasoning_effort="medium"', command)
            self.assertIn('--ignore-user-config', command)
            for feature in ('multi_agent', 'multi_agent_v2', 'shell_tool', 'unified_exec'):
                self.assertEqual(command[command.index(feature) - 1], '--disable')
            Path(command[command.index('--output-last-message') + 1]).write_text(encoded(
                {'assessment': 'routine', 'issues': [], 'escalation': []}))
            return subprocess.CompletedProcess(command, 0)

        with patch.dict(os.environ, {}, clear=True), patch('review.shutil.which', return_value='/fixture/codex'):
            status = review.run(self.dispatcher, 'doc-1', result, runner=run)
            self.assertEqual(status['state'], 'reviewed')
            with self.assertRaisesRegex(DispatchError, 'already exists'):
                review.run(self.dispatcher, 'doc-1', result, runner=run)

    def test_review_timeout_is_uncertain_and_not_retried(self):
        result = self.ready_review()
        with patch.dict(os.environ, {}, clear=True), patch('review.shutil.which', return_value='/fixture/codex'):
            with self.assertRaises(subprocess.TimeoutExpired):
                review.run(self.dispatcher, 'doc-1', result, runner=lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired('fixture', 1)))
        self.assertEqual(self.dispatcher.db.execute('SELECT state FROM review').fetchone()[0], 'uncertain')

    def test_confirmed_medium_review_allows_only_one_explicit_escalation(self):
        initial = self.ready_review()

        def run(command, **kwargs):
            Path(command[command.index('--output-last-message') + 1]).write_text(encoded(
                {'assessment': 'needs_escalation', 'issues': ['Fixture ambiguity'], 'escalation': ['source_ambiguity']}))
            return subprocess.CompletedProcess(command, 0)

        with patch.dict(os.environ, {}, clear=True), patch('review.shutil.which', return_value='/fixture/codex'):
            self.assertEqual(review.run(self.dispatcher, 'doc-1', initial, runner=run)['state'], 'needs_escalation')
            raised = review.plan(self.dispatcher, 'doc-1', 'gpt-6.1-sol', ['source_ambiguity'], 'New evidence.')
            review.run(self.dispatcher, 'doc-1', raised, runner=run, escalate=True)
            with self.assertRaisesRegex(DispatchError, 'already exists'):
                review.run(self.dispatcher, 'doc-1', raised, runner=run, escalate=True)
        self.assertEqual(self.dispatcher.db.execute('SELECT COUNT(*) FROM review').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
