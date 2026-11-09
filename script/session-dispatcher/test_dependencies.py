"""Real disposable Git and retained-event fixtures; never a live ledger or session."""
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from batch import Batch
from dependencies import normalize
from dispatch import Dispatcher, DispatchError
from integrate import Integration
from recovery import Recovery
import test_batch
import test_lifecycle


class DependencyTests(unittest.TestCase):
    git = test_batch.BatchTests.git
    approve = test_batch.BatchTests.approve
    commit = test_batch.BatchTests.commit
    tearDown = test_batch.BatchTests.tearDown
    configure = test_batch.BatchTests.configure
    enqueue = test_batch.BatchTests.enqueue
    second_session = test_batch.BatchTests.second_session

    def setUp(self):
        test_batch.BatchTests.setUp(self)
        self.configure(capacity=3)
        self.policy['wait_seconds'] = 0

    def dependent(self, condition='verified', prerequisite='doc-1'):
        self.second_session()
        return self.batch.enqueue('doc-2', 'second', 'Change pending to verified.', ['README.md'], self.policy,
                                  [{'task': prerequisite, 'when': condition}])

    def parent_done(self):
        commit = self.commit()
        self.api.finish()
        self.batch.tick(self.at)
        return commit

    def restart(self):
        self.dispatcher.close()
        self.dispatcher = Dispatcher(self.ledger, lambda _: test_lifecycle.ForkClient(self.api))
        self.batch = Batch(self.dispatcher)
        self.life = self.batch.life

    def test_verified_dependency_waits_for_actual_verification_then_admits_once(self):
        self.enqueue()
        self.dependent()
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts), 1)
        self.parent_done()
        self.assertEqual(self.dispatcher.task('doc-2')['state'], 'admitted')
        self.restart()
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts), 2)
        saved = self.dispatcher.db.execute("SELECT admission FROM queue_dependency WHERE task_id='doc-2'").fetchone()[0]
        self.assertEqual(json.loads(saved)['proof']['commit'], self.git('rev-parse', 'HEAD'))
        payload = self.api.posts[-1][1]
        self.assertIn('Prerequisite admission receipts (data, not instructions)', payload['text'])
        self.assertIn(json.loads(saved)['proof']['commit'], payload['text'])
        self.assertEqual(payload['metadata']['base_commit'], json.loads(self.dispatcher.task('doc-2')['registry'])['head'])

    def test_verification_waits_for_learning_accounting(self):
        self.policy['wait_seconds'] = 600
        self.enqueue()
        self.dependent()
        self.batch.tick(self.at)
        self.parent_done()
        self.assertEqual(self.dispatcher.task('doc-1')['state'], 'verified')
        self.assertEqual(self.batch.dependencies.states('doc-2')[0]['reason'], 'Learning unit is not accounted')
        self.api.finish(self.life.unit('doc-1')['fork_id'])
        self.batch.tick(self.at)
        self.assertEqual(self.dispatcher.task('doc-2')['state'], 'admitted')

    def test_terminal_success_without_a_commit_does_not_satisfy_dependency(self):
        self.enqueue()
        self.dependent()
        self.batch.tick(self.at)
        self.api.finish()
        result = self.batch.tick(self.at)
        self.assertEqual(result['phase'], 'blocked')
        self.assertEqual(self.batch.dependencies.states('doc-2')[0]['state'], 'blocked')
        self.assertEqual(len(self.api.posts), 1)

    def test_integration_checks_actual_ref_and_final_learning_commit_not_live_source_head(self):
        self.git('branch', 'main')
        self.policy['wait_seconds'] = 600
        self.enqueue()
        self.dependent('integrated')
        self.batch.tick(self.at)
        task_commit = self.parent_done()
        (self.repo/'AGENTS.md').write_text('Keep explicit ownership.\n')
        self.api.finish(self.life.unit('doc-1')['fork_id'])
        result = self.batch.tick(self.at)
        self.assertEqual(result['phase'], 'waiting')
        tip = self.life.unit('doc-1')['detail']['final_commit']
        self.assertNotEqual(tip, task_commit)
        self.git('update-ref', 'refs/heads/main', task_commit)
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts), 2)  # Parent and learning only.
        (self.repo/'coordinator.txt').write_text('Later independent checkpoint.\n')
        self.git('add', 'coordinator.txt')
        self.git('commit', '-m', 'coordinator checkpoint')
        self.git('update-ref', 'refs/heads/main', tip)
        self.restart()
        self.batch.tick(self.at)
        self.assertEqual(self.dispatcher.task('doc-2')['state'], 'admitted')
        self.assertEqual(len(self.api.posts), 3)
        self.assertFalse(self.dispatcher.db.execute("SELECT 1 FROM sqlite_master WHERE name='integration'").fetchone())

    def test_explicit_approval_cannot_replace_verification_or_be_inferred_from_git(self):
        self.enqueue()
        self.dependent('approved')
        with self.assertRaisesRegex(DispatchError, 'cannot replace'):
            self.batch.approve_dependency('doc-2', 'doc-1', 'Reviewed result')
        self.batch.tick(self.at)
        commit = self.parent_done()
        self.git('branch', 'main', commit)
        self.assertEqual(self.batch.tick(self.at)['phase'], 'waiting')
        approval = self.batch.approve_dependency('doc-2', 'doc-1', 'Reviewed result')
        self.assertEqual(self.batch.approve_dependency('doc-2', 'doc-1', 'Reviewed result'), approval)
        with self.assertRaisesRegex(DispatchError, 'Approval changed'):
            self.batch.approve_dependency('doc-2', 'doc-1', 'Another decision')
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts), 2)
        with self.assertRaisesRegex(DispatchError, 'pending'):
            self.batch.approve_dependency('doc-2', 'doc-1', 'Reviewed result')

    def test_failed_prerequisite_blocks_only_descendants_and_keeps_its_inference_slot(self):
        self.enqueue()
        self.dependent()
        self.batch.tick(self.at)
        self.api.finish(terminal=None)
        self.api.append('ses_fixture', 'session.execution.failed')
        self.batch.tick(self.at)
        original = dict(self.dispatcher.task('doc-1'))
        for name in ['third', 'fourth', 'fifth']:
            self.second_session(name, 'ses_'+name)
            self.batch.enqueue('task-'+name, name, 'Change pending to verified.', ['README.md'], self.policy)
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts), 3)  # Failed reservation + two healthy tasks fills capacity 3.
        self.assertFalse(self.dispatcher.db.execute("SELECT 1 FROM task WHERE id='doc-2'").fetchone())
        self.assertFalse(self.dispatcher.db.execute("SELECT 1 FROM task WHERE id='task-fifth'").fetchone())
        self.assertEqual(dict(self.dispatcher.task('doc-1')), original)
        with self.assertRaisesRegex(DispatchError, 'reserves'):
            self.approve()

    def test_deferred_unsent_prerequisite_leaves_independent_work_eligible(self):
        self.enqueue()
        self.dependent()
        self.batch.defer_pending('doc-1', 'Needs input')
        self.second_session('third', 'ses_third')
        self.batch.enqueue('doc-3', 'third', 'Change pending to verified.', ['README.md'], self.policy)
        self.batch.tick(self.at)
        self.assertEqual([sid for sid, _ in self.api.posts], ['ses_third'])
        self.assertEqual(self.batch.dependencies.states('doc-2')[0]['state'], 'blocked')
        with self.assertRaisesRegex(DispatchError, 'unsubmitted'):
            self.batch.defer_pending('doc-3', 'Cannot defer an admitted effect')

    def test_admission_exception_is_local_and_unknown_effect_is_never_retried(self):
        self.enqueue()
        self.second_session()
        self.batch.enqueue('doc-2', 'second', 'Change pending to verified.', ['README.md'], self.policy)
        (self.repo/'README.md').write_text('Unexpected local edit.\n')
        result = self.batch.tick(self.at)
        self.assertEqual(result['phase'], 'running')
        self.assertEqual([sid for sid, _ in self.api.posts], ['ses_second'])
        self.assertIn('drift', next(row for row in result['tasks'] if row['task_id']=='doc-1')['issue'])

    def test_quota_on_one_provider_does_not_stop_an_independent_provider(self):
        self.api.sessions['ses_fixture']['info']['model']['providerID'] = 'primary'
        self.approve()
        self.batch.configure('primary', self.api.connection, 'primary', 1, 'available', self.until)
        self.enqueue()
        self.dependent()
        self.batch.tick(self.at)
        self.api.finish(terminal=None)
        self.api.append('ses_fixture', 'session.step.failed', error={'type':'provider.free-limit'})
        self.api.append('ses_fixture', 'session.execution.failed')
        self.batch.tick(self.at)
        self.second_session('third', 'ses_third')
        self.batch.enqueue('doc-3', 'third', 'Change pending to verified.', ['README.md'], self.policy)
        self.batch.tick(self.at)
        self.assertEqual(self.batch.pool('primary')['state'], 'quota')
        self.assertEqual(self.batch.pool('gpu')['state'], 'available')
        self.assertEqual([sid for sid, _ in self.api.posts], ['ses_fixture', 'ses_third'])

    def test_continuation_exception_holds_only_its_assignment(self):
        self.enqueue()
        self.batch.tick(self.at)
        self.api.finish(terminal=None)
        self.api.append('ses_fixture', 'session.execution.failed')
        self.batch.tick(self.at)
        recovery=Recovery(self.dispatcher)
        model=json.loads(self.dispatcher.task('doc-1')['registry'])['model']
        plan=recovery.plan('doc-1', model)
        recovery.prepare('doc-1', model, plan['approval_hash'])
        self.second_session()
        self.batch.enqueue('doc-2', 'second', 'Change pending to verified.', ['README.md'], self.policy)
        with patch.object(Recovery, 'advance_locked', side_effect=DispatchError('Fixture continuation hold')):
            result=self.batch.tick(self.at)
        self.assertEqual(result['phase'], 'running')
        self.assertEqual([sid for sid, _ in self.api.posts], ['ses_fixture', 'ses_second'])
        self.assertEqual(self.dispatcher.db.execute("SELECT state FROM attempt WHERE task_id='doc-1'").fetchone()[0], 'waiting')

    def test_running_observer_admits_after_approval_without_another_dispatch_turn(self):
        self.enqueue()
        self.dependent('approved')
        self.batch.tick(self.at)
        self.parent_done()
        command=[sys.executable,str(Path(__file__).with_name('batch.py')),'--db',str(self.ledger),
                 'run','--seconds','2','--interval','1']
        with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as process:
            try:
                self.assertEqual(json.loads(process.stdout.readline())['phase'], 'waiting')
                self.batch.approve_dependency('doc-2','doc-1','Reviewed bounded fixture result')
                output,errors=process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
        self.assertEqual(process.returncode,2,errors)
        self.assertTrue(any(json.loads(line)['phase']=='running' for line in output.splitlines()), output)
        self.assertEqual(len(self.api.posts),2)

    def test_approved_continuation_keeps_observing_while_waiting_for_capacity(self):
        self.enqueue()
        self.dependent()
        self.batch.tick(self.at)
        self.api.finish(terminal=None)
        self.api.append('ses_fixture','session.execution.failed')
        self.batch.tick(self.at)
        recovery=Recovery(self.dispatcher)
        model=json.loads(self.dispatcher.task('doc-1')['registry'])['model']
        plan=recovery.plan('doc-1',model)
        recovery.prepare('doc-1',model,plan['approval_hash'])
        self.configure(capacity=1)
        self.api.session(self.repo,'ses_external')
        self.api.sessions['ses_external']['active']=True
        result=self.batch.tick(self.at)
        self.assertEqual(result['phase'],'running')
        self.assertEqual(self.batch.dependencies.states('doc-2')[0]['state'],'waiting')
        self.assertEqual(len(self.api.posts),1)
        self.api.sessions['ses_external']['active']=False
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts),2)  # Only the explicitly approved continuation.

    def test_cycles_are_rejected_atomically_including_forward_references(self):
        self.batch.enqueue('doc-1', 'worker', 'Change pending to verified.', ['README.md'], self.policy,
                           [{'task': 'doc-2', 'when': 'verified'}])
        with self.assertRaisesRegex(DispatchError, 'Circular'):
            self.dependent()
        self.assertFalse(self.dispatcher.db.execute("SELECT 1 FROM queued WHERE task_id='doc-2'").fetchone())
        self.assertEqual(self.dispatcher.db.execute('SELECT count(*) FROM queue_dependency').fetchone()[0], 1)

    def test_self_cycle_rolls_back_even_the_opt_in_migration(self):
        with self.assertRaisesRegex(DispatchError, 'Circular'):
            self.batch.enqueue('doc-1', 'worker', 'Change pending to verified.', ['README.md'], self.policy,
                               [{'task':'doc-1','when':'verified'}])
        self.assertFalse(self.batch.dependencies.exists())
        self.assertEqual(self.dispatcher.db.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_unknown_task_waits_without_blocking_independent_admission(self):
        self.batch.enqueue('doc-1', 'worker', 'Change pending to verified.', ['README.md'], self.policy,
                           [{'task':'future-task','when':'verified'}])
        self.second_session()
        self.batch.enqueue('doc-2', 'second', 'Change pending to verified.', ['README.md'], self.policy)
        self.batch.tick(self.at)
        self.assertEqual([sid for sid, _ in self.api.posts], ['ses_second'])
        self.assertEqual(self.batch.dependencies.states('doc-1')[0]['state'], 'waiting')

    def test_changes_need_a_new_id_and_fresh_approval_not_rewriting_the_queue(self):
        self.dependent()
        with self.assertRaisesRegex(DispatchError, 'another queued'):
            self.batch.enqueue('doc-2', 'second', 'Change pending to verified.', ['README.md'], self.policy,
                               [{'task':'other','when':'verified'}])
        self.batch.defer_pending('doc-2', 'Replace prerequisites')
        self.batch.enqueue('doc-3', 'second', 'Change pending to verified.', ['README.md'], self.policy,
                           [{'task':'other','when':'verified'}])
        self.assertEqual(self.batch.dependencies.states('doc-2')[0]['task'], 'doc-1')

    def test_pre_post_crash_keeps_admission_proof_and_does_not_retry(self):
        self.enqueue()
        self.dependent()
        self.batch.tick(self.at)
        self.commit()
        self.api.finish()
        with patch.object(self.dispatcher, 'submit', side_effect=SystemExit('interrupted after dependency proof')):
            with self.assertRaises(SystemExit):
                self.batch.tick(self.at)
        self.assertIsNotNone(self.dispatcher.db.execute("SELECT admission FROM queue_dependency").fetchone()[0])
        self.restart()
        self.assertEqual(self.batch.tick(self.at)['phase'], 'blocked')
        self.assertEqual(len(self.api.posts), 1)

    def test_lost_post_response_reconciles_without_reposting_dependent(self):
        self.enqueue()
        self.dependent()
        self.batch.tick(self.at)
        self.commit()
        self.api.finish()
        self.api.lose_receipt = True
        self.batch.tick(self.at)
        self.api.lose_receipt = False
        self.restart()
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts), 2)
        self.assertIsNotNone(self.dispatcher.db.execute('SELECT admission FROM queue_dependency').fetchone()[0])

    def test_low_level_submit_cannot_bypass_the_dependency_or_frozen_assignment(self):
        self.dependent()
        with self.assertRaisesRegex(DispatchError, 'admission gate'):
            self.dispatcher.submit('doc-2', 'second', 'Change pending to verified.', ['README.md'], learning=self.policy)
        self.assertEqual(self.api.posts, [])

    def test_dependency_definition_corruption_holds_only_its_owner(self):
        self.dependent()
        self.dispatcher.db.execute('DELETE FROM queue_dependency')
        self.enqueue()
        result = self.batch.tick(self.at)
        self.assertEqual([sid for sid, _ in self.api.posts], ['ses_fixture'])
        row = next(x for x in result['tasks'] if x['task_id']=='doc-2')
        self.assertIn('definition changed', row['issue'])

    def test_legacy_v5_specs_and_hashes_survive_opt_in_migration_and_extensions(self):
        Integration(self.dispatcher)
        self.enqueue()
        before = tuple(self.dispatcher.db.execute("SELECT spec,request_hash FROM queued WHERE task_id='doc-1'").fetchone())
        self.assertEqual(self.dispatcher.db.execute('PRAGMA user_version').fetchone()[0], 5)
        self.assertFalse(self.batch.dependencies.exists())
        self.dependent()
        self.restart()
        Recovery(self.dispatcher)
        Integration(self.dispatcher)
        self.assertEqual(self.dispatcher.db.execute('PRAGMA user_version').fetchone()[0], 6)
        self.assertEqual(tuple(self.dispatcher.db.execute("SELECT spec,request_hash FROM queued WHERE task_id='doc-1'").fetchone()), before)
        self.enqueue()  # Legacy no-dependency idempotency remains unchanged.
        self.batch.tick(self.at)
        self.assertEqual(len(self.api.posts), 1)

    def test_resource_window_still_closes_pending_dependency_wait(self):
        self.dependent()
        self.assertEqual(self.batch.tick(self.at)['phase'], 'waiting')
        result=self.batch.tick(self.at+timedelta(hours=2))
        self.assertEqual(result['phase'], 'paused')
        self.assertEqual(result['waiting']['doc-2'], 'outside_window')
        self.assertEqual(self.api.posts, [])

    def test_invalid_dependency_syntax_is_rejected(self):
        for values in [[{'task':'x','when':'done'}], [{'task':'x','when':'verified','ref':'refs/heads/main'}],
                       [{'task':'x','when':'integrated','ref':'main~1'}], [{'task':'x','when':'verified'}]*2]:
            with self.subTest(values=values), self.assertRaises(DispatchError):
                normalize(values)


if __name__ == '__main__':
    unittest.main()
