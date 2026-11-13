"""Disposable Git worktrees and fake V2 API; no production worker or inference."""
import copy
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from autonomy import Autonomy, proposal
from dispatch import DispatchError, digest
from operations import report
from runner import run
import test_batch
import test_shadow


class AutonomyTests(unittest.TestCase):
    git = test_batch.BatchTests.git
    approve = test_batch.BatchTests.approve
    tearDown = test_batch.BatchTests.tearDown
    configure = test_batch.BatchTests.configure
    second_session = test_batch.BatchTests.second_session

    def setUp(self):
        test_batch.BatchTests.setUp(self)
        (self.repo/'PLAN.md').write_text('Existing bounded correction work.\n')
        (self.repo/'HANDOVER.md').write_text('Reuse frozen inputs and preserve evidence.\n')
        self.git('add','PLAN.md','HANDOVER.md');self.git('commit','-m','Fixture roadmap')
        self.approve('worker','ses_fixture',self.repo)
        self.api.creates=[];self.api.lose_create=False
        self.dispatcher.client_factory=lambda _:test_shadow.Client(self.api)
        self.aroot=self.root/'autonomy';self.aroot.mkdir(mode=0o700)
        reg=json.loads(self.dispatcher.db.execute("SELECT snapshot FROM registry WHERE name='worker'").fetchone()[0])
        self.c={'version':1,'id':'fixture','root':str(self.aroot),'database':str(self.ledger),
                'repository':str(self.repo),'base':self.git('rev-parse','HEAD'), 'scopes':{'tests':'Deliver existing correction tests'},
                'until':self.until,'pool':'gpu','model':reg['model'],'connection':str(self.api.connection),
                'workers':['worker'],'protected_worktrees':[],'service_state':str(self.root),
                'learning':{**self.policy,'wait_seconds':0},'max_worktrees':12,'max_tasks':32,'backlog':6,'seeds':[]}
        self.auto=Autonomy(self.dispatcher,self.c,activity=lambda root:True)
        self.configure(capacity=4)

    def item(self,key='fix-test-one',dependencies=None):
        return {'key':key,'scope':'tests','kind':'fixture',
                'objective':'Implement the concrete missing regression fixture with retained offline proof.',
                'references':['README.md'],'dependencies':dependencies or []}

    def finish(self, task, followups=None):
        row=self.dispatcher.task(task);reg=json.loads(row['registry']);tree=Path(reg['worktree'])
        record=tree/'integration'/task;record.mkdir(parents=True)
        (record/'README.md').write_text('Fixture implementation complete.\n')
        (record/'followups.json').write_text(json.dumps({'tasks':followups or [],'reason':'Exact remaining fixture gap.'}))
        subprocess.run(['git','-C',str(tree),'add','integration'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(tree),'commit','-m','Fixture result','-m','OpenCode-Task: '+task],check=True,capture_output=True)
        tip=subprocess.check_output(['git','-C',str(tree),'rev-parse','HEAD'],text=True).strip()
        self.api.finish(reg['session_id'])
        self.configure(state='paused',capacity=4)
        self.batch.tick(self.at)  # Wait=0 records unavailable learning rather than requiring fake CLI fork support.
        self.configure(capacity=4)
        self.assertEqual(self.auto.batch.life.unit(task)['stage'],'ready')
        return tree,tip

    def test_reuse_only_waits_for_integration_then_reuses_the_same_worker(self):
        self.git('branch','main',self.c['base'])
        self.c.update(reuse_only=True,reuse_ref='refs/heads/main')
        self.auto.add(self.item(),'operator-policy');self.auto.tick();self.batch.tick(self.at)
        task=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE key='fix-test-one'").fetchone()[0]
        tree,tip=self.finish(task,[self.item('next-existing-worker')])
        self.auto.tick()
        row=self.dispatcher.db.execute("SELECT state,detail FROM autonomy_item WHERE key='next-existing-worker'").fetchone()
        self.assertEqual(row['state'],'pending')
        self.assertEqual(json.loads(row['detail'])['wait'],'approved_worktree_unavailable')
        self.assertEqual(len(self.api.creates),0)
        self.git('update-ref','refs/heads/main',tip,self.c['base'])  # Fixture integration.
        self.auto.tick();self.batch.tick(self.at)
        second=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE key='next-existing-worker'").fetchone()[0]
        self.assertEqual(self.dispatcher.task(second)['worktree'],str(self.repo))
        self.assertEqual(len(self.api.creates),0)
        self.assertEqual(self.git('branch','--show-current'),'fixture')
        self.assertEqual(self.git('rev-parse','HEAD'),tip)

    def test_reuse_only_never_allocates_a_replacement_for_a_protected_worker(self):
        self.git('branch','main',self.c['base'])
        self.c.update(reuse_only=True,reuse_ref='refs/heads/main',protected_worktrees=[str(self.repo)])
        self.auto.add(self.item(),'operator-policy');self.auto.tick();self.auto.tick()
        self.assertEqual(len(self.api.creates),0)
        self.assertEqual(len(self.api.posts),0)
        self.assertEqual(self.dispatcher.db.execute("SELECT state FROM autonomy_item WHERE key='fix-test-one'").fetchone()[0],'pending')
        with self.assertRaisesRegex(DispatchError,'forbids creating'):
            self.auto.new_worker('unexpected','auto-forbidden')

    def test_three_completion_cycles_refill_without_a_planning_operator(self):
        self.auto.add(self.item(),'operator-policy')
        original=[]
        for n in range(3):
            self.auto.tick();self.batch.tick(self.at)
            task=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE state='queued' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            next_item=self.item('fix-followup-'+str(n))
            original.append(self.finish(task,[next_item]))
            self.auto=Autonomy(self.dispatcher,self.c,activity=lambda root:True)  # Process restart.
        self.auto.tick();self.batch.tick(self.at)
        self.assertEqual(self.dispatcher.db.execute("SELECT count(*) FROM autonomy_seen").fetchone()[0],3)
        self.assertEqual(self.dispatcher.db.execute("SELECT count(*) FROM queued WHERE state='active'").fetchone()[0],1)
        self.assertEqual(len(self.api.posts),4)
        for tree,tip in original:
            self.assertEqual(subprocess.check_output(['git','-C',str(tree),'rev-parse','HEAD'],text=True).strip(),tip)
        self.assertEqual(self.dispatcher.db.execute("SELECT count(*) FROM autonomy_item WHERE json_extract(detail,'$.new_worktree') IS NOT NULL").fetchone()[0],3)

    def test_duplicate_key_and_restart_do_not_duplicate_dispatch(self):
        self.auto.add(self.item(),'operator-policy');self.auto.tick();self.batch.tick(self.at)
        self.auto.tick();self.batch.tick(self.at)
        # A planner may queue independently, but never resubmits the original task.
        task='auto-'+digest([self.c['id'],'fix-test-one'])[:24]
        self.assertEqual(self.dispatcher.db.execute('SELECT count(*) FROM queued WHERE task_id=?',(task,)).fetchone()[0],1)
        changed=self.item();changed['objective']+=' Different request.'
        with self.assertRaisesRegex(DispatchError,'different proposal'):self.auto.add(changed,'agent')

    def test_one_rejected_proposal_does_not_block_an_independent_one(self):
        self.auto.add(self.item(),'operator-policy');self.auto.tick();self.batch.tick(self.at)
        task=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE key='fix-test-one'").fetchone()[0]
        bad=self.item('bad-scope');bad['scope']='new-discovery'
        self.finish(task,[bad,self.item('good-followup')]);self.auto.tick()
        row=self.dispatcher.db.execute("SELECT detail FROM autonomy_seen WHERE task_id=?",(task,)).fetchone()
        self.assertEqual(len(json.loads(row[0])['rejected']),1)
        self.assertIsNotNone(self.dispatcher.db.execute("SELECT 1 FROM autonomy_item WHERE key='good-followup'").fetchone())

    def test_no_work_planner_is_not_repeated_by_clock_ticks(self):
        self.auto.tick();self.batch.tick(self.at)
        task=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE source='planner'").fetchone()[0]
        self.finish(task,[]);self.auto.tick();self.auto.tick()
        self.assertEqual(self.dispatcher.db.execute("SELECT count(*) FROM autonomy_item WHERE source='planner'").fetchone()[0],1)

    def test_followup_can_pin_a_new_artifact_in_its_verified_parent(self):
        self.auto.add(self.item(),'operator-policy');self.auto.tick();self.batch.tick(self.at)
        task=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE key='fix-test-one'").fetchone()[0]
        child=self.item('validate-new-artifact');child['references']=['integration/'+task+'/README.md']
        tree,tip=self.finish(task,[child]);self.auto.tick()
        row=self.dispatcher.db.execute("SELECT detail,payload FROM autonomy_item WHERE key='validate-new-artifact'").fetchone()
        self.assertEqual(json.loads(row[0])['reference_origins'],{child['references'][0]:tip})
        missing=self.item('missing-parent-file');missing['references']=['integration/not-a-real-file']
        with self.assertRaisesRegex(DispatchError,'Reference missing'):self.auto.add(missing,task)

    def test_lost_create_reply_is_held_and_never_replayed(self):
        self.c['workers']=[];self.api.lose_create=True
        self.auto.add(self.item(),'operator-policy');self.auto.tick()
        self.assertEqual(len(self.api.creates),1)
        # Stop unrelated proposal generation in this fixture; the interrupted item's intent stays held.
        self.c['until']=(self.at-timedelta(seconds=1)).isoformat()
        self.auto.tick()
        self.assertEqual(len(self.api.creates),1)
        self.assertEqual(self.dispatcher.db.execute("SELECT state FROM autonomy_item WHERE key='fix-test-one'").fetchone()[0],'held')

    def test_protected_or_active_existing_tree_is_not_written(self):
        self.c['protected_worktrees']=[str(self.repo)]
        before=self.git('rev-parse','HEAD')
        self.auto.add(self.item(),'operator-policy');self.auto.tick()
        task=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE key='fix-test-one'").fetchone()[0]
        reg=json.loads(self.dispatcher.db.execute('SELECT snapshot FROM registry WHERE name=?',(task,)).fetchone()[0])
        self.assertNotEqual(reg['worktree'],str(self.repo));self.assertEqual(self.git('rev-parse','HEAD'),before)

    def test_approval_dependency_survives_refill_and_independent_work_admits(self):
        self.auto.add(self.item('prerequisite'),'operator-policy');self.auto.tick();self.batch.tick(self.at)
        parent=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE key='prerequisite'").fetchone()[0]
        self.finish(parent,[])
        self.auto.add(self.item('gated-work',[{'task':parent,'when':'approved'}]),parent)
        self.auto.add(self.item('independent-work'),parent)
        self.auto.tick();self.batch.tick(self.at)
        items=dict(self.dispatcher.db.execute('SELECT key,task_id FROM autonomy_item'))
        self.assertEqual(self.dispatcher.db.execute('SELECT state FROM queued WHERE task_id=?',(items['gated-work'],)).fetchone()[0],'pending')
        self.assertEqual(self.dispatcher.db.execute('SELECT state FROM queued WHERE task_id=?',(items['independent-work'],)).fetchone()[0],'active')

    def test_malformed_scope_paths_and_recipe_are_rejected(self):
        for mutate in [lambda v:v.update(kind='native'),lambda v:v.update(scope='unknown'),
                       lambda v:v.update(references=['../outside']),lambda v:v.update(key='../../escape')]:
            value=self.item();mutate(value)
            with self.assertRaises(DispatchError):proposal(value,self.c)

    def test_completed_inference_releases_only_capacity_not_the_dirty_unit(self):
        self.batch.enqueue('old-completed','worker','Complete without a commit.',['README.md'],self.policy)
        self.batch.tick(self.at);self.api.finish();self.batch.tick(self.at)
        self.assertEqual(self.batch.life.unit('old-completed')['stage'],'needs_review')
        pool=report(self.ledger,live=True,client_factory=self.dispatcher.client_factory)['pools'][0]
        self.assertEqual(pool['occupied'],0)
        with self.assertRaisesRegex(DispatchError,'reserves'):
            self.dispatcher.check_lease(__import__('dispatch').identity(self.repo))
        # New foreign input destroys the settled-tail proof; it must reserve inference again.
        self.api.append('ses_fixture','session.inbox.enqueued',inboxID='foreign',item={'type':'user'})
        pool=report(self.ledger,live=True,client_factory=self.dispatcher.client_factory)['pools'][0]
        self.assertEqual(pool['occupied'],1)

    def test_explicit_weekend_observer_window_is_supported(self):
        deadline=(self.at+timedelta(days=3)).isoformat()
        with patch('batch.Batch.tick',return_value={'phase':'complete'}):
            self.assertEqual(run(self.ledger,run_dir=self.root/'weekend-run',until=deadline),0)
        with self.assertRaises(DispatchError):
            run(self.ledger,run_dir=self.root/'too-long',until=(self.at+timedelta(days=8)).isoformat())

    def test_policy_hash_is_frozen_on_observer_restart(self):
        path=self.aroot/'policy.json';path.write_text(json.dumps(self.c));path.chmod(0o600)
        run(self.ledger,run_dir=self.root/'policy-run',seconds=0,autonomy=str(path))
        altered={**self.c,'max_tasks':31};path.write_text(json.dumps(altered))
        with self.assertRaisesRegex(DispatchError,'policy changed'):
            run(self.ledger,run_dir=self.root/'policy-run')

    def test_native_recipe_needs_both_a_profile_and_an_explicit_scope(self):
        value=self.item();value['kind']='ol8-tests'
        self.c['native_profiles']={'ol8-tests':'Only the accepted payload in a new isolated worker.'}
        with self.assertRaisesRegex(DispatchError,'Native scope'):proposal(value,self.c)
        self.c['native_scopes']=['tests']
        self.assertEqual(proposal(value,self.c)['kind'],'ol8-tests')
        self.c['native_profiles']['ol8-tests']='Create only whack-TASK_ID in builds/TASK_ID.'
        prompt=self.auto.prompt(value,'auto-fixture','operator-policy')
        self.assertIn('whack-auto-fixture',prompt)
        self.assertNotIn('TASK_ID',prompt)

    def test_uncertain_native_scope_cannot_be_retried_under_another_key(self):
        self.c['native_profiles']={'ol8-tests':'Only an accepted payload in a new isolated worker.'}
        self.c['scopes']['other']='A different existing correction.'
        self.c['native_scopes']=['tests','other']
        value=self.item('native-first');value['kind']='ol8-tests'
        self.auto.add(value,'operator-policy');self.auto.tick();self.batch.tick(self.at)
        task=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE key='native-first'").fetchone()[0]
        binding=json.loads(self.dispatcher.task(task)['registry'])
        self.api.finish(binding['session_id'],terminal=None)
        self.api.append(binding['session_id'],'session.execution.failed')
        self.batch.tick(self.at)
        retry={**value,'key':'native-first-with-a-new-name'}
        with self.assertRaisesRegex(DispatchError,'Native effects in this scope'):self.auto.add(retry,task)
        self.auto.add(self.item('offline-inspection'),'operator-policy')
        independent={**value,'key':'native-other-scope','scope':'other'}
        self.auto.add(independent,'operator-policy');self.auto.tick();self.batch.tick(self.at)
        other=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE key='native-other-scope'").fetchone()[0]
        self.assertEqual(self.dispatcher.task(other)['state'],'admitted')

    def test_verified_dependency_artifact_is_pinned_through_a_review_parent(self):
        self.auto.add(self.item('original-export'),'operator-policy');self.auto.tick();self.batch.tick(self.at)
        first=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE key='original-export'").fetchone()[0]
        review=self.item('independent-review',[{'task':first,'when':'verified'}])
        tree,tip=self.finish(first,[review]);self.auto.tick();self.batch.tick(self.at)
        second=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE key='independent-review'").fetchone()[0]
        child=self.item('consume-reviewed-export');child['references']=['integration/'+first+'/README.md']
        self.finish(second,[child]);self.auto.tick()
        row=self.dispatcher.db.execute("SELECT detail FROM autonomy_item WHERE key='consume-reviewed-export'").fetchone()
        self.assertEqual(json.loads(row[0])['reference_origins'],{child['references'][0]:tip})

    def test_planner_can_reference_a_declared_verified_dependency_output(self):
        self.auto.add(self.item('export-first'),'operator-policy');self.auto.tick();self.batch.tick(self.at)
        first=self.dispatcher.db.execute("SELECT task_id FROM autonomy_item WHERE key='export-first'").fetchone()[0]
        tree,tip=self.finish(first,[])
        child=self.item('planner-consumes-output',[{'task':first,'when':'verified'}])
        child['references']=['integration/'+first+'/README.md']
        self.auto.add(child,'planner')
        row=self.dispatcher.db.execute("SELECT detail FROM autonomy_item WHERE key='planner-consumes-output'").fetchone()
        self.assertEqual(json.loads(row[0])['reference_origins'],{child['references'][0]:tip})


if __name__=='__main__':unittest.main()
