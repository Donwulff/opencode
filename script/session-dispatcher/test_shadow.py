"""Disposable local Git/API fixtures only; no real sessions or inference."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from dispatch import DispatchError, digest
from operations import report, review_ready
from shadow import Shadow, assessment
import test_batch
import test_lifecycle
import test_operations


class Client(test_lifecycle.ForkClient):
    def json(self, method, path, payload=None):
        if method == 'POST' and path == '/api/session':
            self.api.creates.append(copy.deepcopy(payload))
            sid = self.api.session(Path(payload['location']['directory']),payload['id'])
            self.api.sessions[sid]['info'].update(payload)
            if self.api.lose_create: raise DispatchError('Lost create reply')
            return {'data':copy.deepcopy(self.api.sessions[sid]['info'])}
        return super().json(method,path,payload)


class ShadowTests(unittest.TestCase):
    git = test_batch.BatchTests.git
    approve = test_batch.BatchTests.approve
    commit = test_batch.BatchTests.commit
    tearDown = test_batch.BatchTests.tearDown
    configure = test_batch.BatchTests.configure
    enqueue = test_batch.BatchTests.enqueue
    ready_for_review = test_operations.OperationsTests.ready_for_review
    second_session = test_batch.BatchTests.second_session

    def setUp(self):
        test_batch.BatchTests.setUp(self)
        self.ready_for_review()
        self.api.creates=[];self.api.lose_create=False
        self.dispatcher.client_factory=lambda _:Client(self.api)
        self.root_shadow=self.root/'shadows';self.root_shadow.mkdir(mode=0o700)
        self.config={'version':1,'database':str(self.ledger),'root':str(self.root_shadow),'repository':str(self.repo),
                     'target_ref':'target','connection':str(self.api.connection),'model':{'providerID':'fixture','id':'learner'},
                     'learning':self.policy,'max_cases':10,'max_disagreements':3}
        self.shadow=Shadow(self.dispatcher,self.config)

    def capture(self):return self.shadow.capture('doc-1')

    def finish(self, case, verdict='routine'):
        self.configure(capacity=2)
        self.batch.tick(self.at)
        root=Path(case['directory'])
        (root/'result.json').write_text(json.dumps({'assessment':verdict,'issues':[],'escalation':[]}))
        (root/'review.md').write_text('Frozen README diff and receipt reviewed. No release claim.\n')
        completed=subprocess.run([sys.executable,'finish.py'],cwd=root,capture_output=True,text=True)
        self.assertEqual(completed.returncode,0,completed.stderr)
        self.api.finish(case['session_id'])
        self.configure(state='paused')
        self.batch.tick(self.at)
        return self.shadow.collect()[0]

    def test_capture_is_fresh_frozen_and_restart_deduplicated(self):
        case=self.capture();body=json.loads((Path(case['directory'])/'input.json').read_text())
        self.assertEqual(digest(body),case['input_hash'])
        self.assertNotEqual(case['session_id'],'ses_fixture')
        self.assertNotIn('parentID',self.api.creates[0]);self.assertNotIn('reference',body)
        self.assertEqual(self.api.forks,0)
        self.assertEqual(Shadow(self.dispatcher,self.config).capture('doc-1'),case)
        self.assertEqual(len(self.api.creates),1)
        self.assertEqual(self.git('rev-parse','HEAD'),body['receipt']['final_commit'])

    def test_lost_session_create_is_not_replayed_or_recreated(self):
        self.api.lose_create=True
        with self.assertRaises(DispatchError):self.capture()
        held=Shadow(self.dispatcher,self.config).capture('doc-1')
        self.assertEqual(held['stage'],'creating_session');self.assertEqual(len(self.api.creates),1)
        self.assertIsNone(self.dispatcher.db.execute("SELECT 1 FROM queued WHERE task_id=?",(held['shadow_task'],)).fetchone())

    def test_delivery_admits_before_older_shadow_and_capacity_is_shared(self):
        case=self.capture()
        self.second_session();self.batch.enqueue('delivery-2','second','Review README.',['README.md'],self.policy)
        self.configure(capacity=1);self.batch.tick(self.at)
        self.assertEqual(self.dispatcher.task('delivery-2')['state'],'admitted')
        self.assertIsNone(self.dispatcher.db.execute('SELECT 1 FROM task WHERE id=?',(case['shadow_task'],)).fetchone())
        self.assertEqual(len(self.api.posts),2)

    def test_result_is_advisory_reference_is_immutable_and_no_recursive_review(self):
        case=self.capture()
        self.shadow.reference(case['id'],case['input_hash'],'blocked','Independent reference; exact fixture gap.')
        self.assertNotIn('reference',json.loads((Path(case['directory'])/'input.json').read_text()))
        result=self.finish(case)
        self.assertTrue(result['disagreement']);self.assertEqual(result['learning'],'skipped_gpu_unavailable')
        self.assertEqual(review_ready(self.ledger,self.repo,'target')['counts'],{'awaiting_review':1})
        shadow_receipt=next(r for r in report(self.ledger)['ready_receipts'] if r['task_id']==case['shadow_task'])
        self.assertEqual(shadow_receipt['purpose'],'shadow_review')
        with self.assertRaisesRegex(DispatchError,'never shadow a shadow'):self.shadow.capture(case['shadow_task'])
        with self.assertRaisesRegex(DispatchError,'immutable'):
            self.shadow.reference(case['id'],case['input_hash'],'routine','Overwrite reference')

    def test_reference_requires_correct_frozen_hash_and_absence_is_not_a_verdict(self):
        case=self.capture()
        with self.assertRaises(DispatchError):self.shadow.reference(case['id'],'bad','routine','reason')
        result=self.finish(case)
        self.assertNotIn('disagreement',result)
        self.assertNotIn('reference',result)

    def test_finisher_refuses_tampered_frozen_files_or_unexpected_outputs(self):
        case=self.capture();root=Path(case['directory'])
        (root/'result.json').write_text(json.dumps({'assessment':'routine','issues':[],'escalation':[]}))
        (root/'review.md').write_text('Evidence.\n');(root/'extra.txt').write_text('Unexpected\n')
        self.assertNotEqual(subprocess.run([sys.executable,'finish.py'],cwd=root,capture_output=True).returncode,0)
        (root/'extra.txt').unlink();(root/'patches/task.diff').write_text('Changed input')
        self.assertNotEqual(subprocess.run([sys.executable,'finish.py'],cwd=root,capture_output=True).returncode,0)
        self.assertEqual(subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),case['base'])

    def test_trial_budget_and_disagreement_stop_only_future_shadow_creation(self):
        case=self.capture();self.config['max_cases']=1
        self.assertEqual(self.capture(),case)
        receipt=dict(report(self.ledger)['ready_receipts'][0],task_id='second-ready')
        with patch('shadow.report',return_value={'ready_receipts':[receipt]}):
            with self.assertRaisesRegex(DispatchError,'case budget'):self.shadow.capture('second-ready')
            self.config['max_cases']=10;case['disagreement']=True;self.shadow.save(case);self.config['max_disagreements']=1
            with self.assertRaisesRegex(DispatchError,'disagreement stop'):self.shadow.capture('second-ready')
        self.assertEqual(len(self.api.creates),1)
        self.assertEqual(self.dispatcher.task('doc-1')['state'],'verified')

    def test_result_schema_rejects_extra_fields_and_unknown_risks(self):
        for value in [{},{'assessment':'routine','issues':[],'escalation':['unknown']},
                      {'assessment':'routine','issues':[],'escalation':[],'merge':True}]:
            with self.assertRaises(DispatchError):assessment(value)

    def test_observer_cannot_overwrite_a_concurrent_reference_writer(self):
        case=self.capture()
        with self.shadow.lock():
            with self.assertRaisesRegex(DispatchError,'Another shadow writer'):
                self.shadow.reference(case['id'],case['input_hash'],'routine','decision')
        self.assertNotIn('reference',self.shadow.cases()[0])


if __name__=='__main__':unittest.main()
