"""Disposable ledger/Git and native-protocol fixtures; no paid inference."""
from datetime import timedelta
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from coordinator import Coordinator, CodexRPC, load_config, lock, same_input
from dispatch import DispatchError
import test_batch
import test_operations


class Native:
    def __init__(self, cwd):
        self.cwd, self.status, self.pending, self.history = str(cwd), 'idle', [], []
        self.calls, self.starts, self.fail = [], 0, None
        self.auto_start = False

    def call(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'thread/read':
            return {'thread': {'cwd': self.cwd, 'status': {'type': self.status}}}
        if method == 'thread/resume':
            self.status = 'idle'
            return {'thread': {'cwd': self.cwd, 'status': {'type': 'idle'}}}
        if method == 'thread/settings/update':
            return {}
        if method == 'thread/queue/list':
            return {'data': copy.deepcopy(self.pending), 'nextCursor': None}
        if method == 'thread/items/list':
            return {'data': copy.deepcopy(self.history), 'nextCursor': None}
        if method == 'thread/queue/add':
            if self.fail == 'add_before':
                self.fail = None
                raise OSError('Lost before admission')
            entry = dict(id='submission-1', clientUserMessageId=params['clientUserMessageId'], input=params['input'])
            for part in entry['input']:
                part.setdefault('text_elements', [])
            self.pending = [entry]
            if self.auto_start:
                self.starts += 1
                self.history += [{'turnId':'turn-fixture','item':{'type':'userMessage','id':'user-fixture','clientId':entry['clientUserMessageId']}}]
                self.pending = []
                self.status = 'active'
            if self.fail == 'add_after':
                self.fail = None
                raise OSError('Lost admission reply')
            return {'queuedSubmission': copy.deepcopy(entry)}
        if method == 'thread/queue/start':
            if self.fail == 'start_before':
                self.fail = None
                raise OSError('Lost before start')
            self.starts += 1
            self.history += [{'turnId':'turn-fixture','item':{'type':'userMessage','id':'user-fixture','clientId':self.pending[0]['clientUserMessageId']}}]
            self.pending = []
            self.status = 'active'
            if self.fail == 'start_after':
                self.fail = None
                raise OSError('Lost start reply after effects')
            return {'turn': {'id': 'turn-fixture', 'status': 'inProgress'}}
        raise AssertionError(method)

    def close(self):
        pass


class CoordinatorTests(unittest.TestCase):
    git = test_batch.BatchTests.git
    approve = test_batch.BatchTests.approve
    commit = test_batch.BatchTests.commit
    tearDown = test_batch.BatchTests.tearDown
    configure = test_batch.BatchTests.configure
    enqueue = test_batch.BatchTests.enqueue
    second_session = test_batch.BatchTests.second_session
    ready_for_review = test_operations.OperationsTests.ready_for_review

    def setUp(self):
        test_batch.BatchTests.setUp(self)
        self.directory = self.root / 'coordinator'
        self.directory.mkdir(mode=0o700)
        self.config = {'version': 1, 'root': str(self.directory), 'database': str(self.ledger),
                       'repository': str(self.repo), 'target_ref': 'target', 'thread_id': 'thread-fixture',
                       'codex_socket': '/fixture/socket', 'bun': shutil.which('bun') or '/fixture/bun',
                       'deadline': (self.at + timedelta(hours=1)).isoformat(), 'interval': 5,
                       'min_ready': 1, 'max_wait': 30, 'cooldown': 60, 'max_wakes': 2}
        self.native = Native(self.repo)
        self.coordinator = Coordinator(self.config, lambda _: self.native)

    def restart(self):
        self.coordinator = Coordinator(self.config, lambda _: self.native)

    def test_real_ledger_handoff_is_frozen_and_wakes_once_without_ledger_or_git_writes(self):
        base, commit = self.ready_for_review()
        before = self.ledger.read_bytes(), self.git('show-ref'), list(self.api.posts)
        self.coordinator.tick(at=self.at)
        e = self.coordinator.state['entries'][0]
        self.assertEqual(e['stage'], 'started')
        value = json.loads(Path(e['handoff']).read_text())
        self.assertEqual(value['target']['commit'], base)
        self.assertEqual(value['units'][0]['final_commit'], commit)
        self.assertEqual(self.native.starts, 1)
        self.native.status = 'idle'
        self.restart()
        self.coordinator.tick(at=self.at + timedelta(minutes=5))
        self.assertEqual(self.native.starts, 1)
        self.assertEqual((self.ledger.read_bytes(), self.git('show-ref'), list(self.api.posts)), before)
        self.assertNotIn('assessment', e)  # A wake receipt never certifies source/Git success.

    def test_native_auto_start_during_admission_is_reconciled_without_explicit_start(self):
        self.ready_for_review()
        self.native.auto_start = True
        self.coordinator.tick(at=self.at)
        entry = self.coordinator.state['entries'][0]
        self.assertEqual(entry['stage'], 'observed_started')
        self.assertEqual(entry['turn_id'], 'turn-fixture')
        self.assertEqual(self.native.starts, 1)
        self.assertFalse(any(m == 'thread/queue/start' for m, _ in self.native.calls))

    def test_actual_native_envelope_and_empty_text_metadata(self):
        fixture = json.loads(Path(__file__).with_name('fixtures').joinpath('codex-queue-native-01672.json').read_text())
        original = [{'type':'text','text':'sanitized coordinator wake'}]
        self.assertTrue(same_input(fixture['queue_add']['queuedSubmission']['input'], original))
        different = copy.deepcopy(original)
        different[0]['text'] += ' changed'
        self.assertFalse(same_input(fixture['queue_add']['queuedSubmission']['input'], different))
        self.native.history = fixture['history']['data']
        result = self.coordinator.consumed(self.native, 'fixture-client-id')
        self.assertEqual(result, {'turn_id':'fixture-turn-id','user_message_id':'fixture-user-id'})
        self.native.history = [{'type':'userMessage','clientId':'fixture-client-id'}]
        with self.assertRaisesRegex(DispatchError, 'Unsupported native history envelope'):
            self.coordinator.consumed(self.native, 'fixture-client-id')

    def test_active_context_and_pending_human_input_prevent_admission(self):
        self.ready_for_review()
        for status, queue in [('active', []), ('notLoaded', []), ('idle', [{'clientUserMessageId': 'human'}])]:
            self.native.status, self.native.pending = status, queue
            self.coordinator.tick(at=self.at)
        self.assertFalse(any(m == 'thread/queue/add' for m, _ in self.native.calls))
        self.assertEqual(self.coordinator.state['entries'], [])

    def test_resume_requires_an_explicit_dedicated_context_policy(self):
        self.ready_for_review()
        self.config['allow_resume'] = True
        self.restart()
        self.native.status = 'notLoaded'
        self.coordinator.tick(at=self.at)
        self.assertEqual(self.native.starts, 1)
        self.assertEqual(sum(m == 'thread/resume' for m, _ in self.native.calls), 1)

    def test_new_user_input_after_admission_is_not_reordered_or_started(self):
        self.ready_for_review()
        self.native.fail = 'add_after'
        with self.assertRaises(OSError):
            self.coordinator.tick(at=self.at)
        self.native.pending.append({'id':'human-submission','clientUserMessageId':'human','input':[]})
        self.restart()
        self.coordinator.tick(at=self.at)
        self.assertEqual(self.native.starts, 0)
        self.assertEqual(len(self.native.pending), 2)
        self.assertEqual(self.coordinator.state['entries'][0]['stage'], 'admitted')

    def test_repository_drift_refuses_any_admission(self):
        self.ready_for_review()
        self.native.cwd = str(self.root / 'other')
        with self.assertRaisesRegex(DispatchError, 'repository drift'):
            self.coordinator.tick(at=self.at)
        self.assertFalse(any(m == 'thread/queue/add' for m, _ in self.native.calls))

    def test_lost_admission_ack_is_recovered_from_native_queue_without_another_add(self):
        self.ready_for_review()
        self.native.fail = 'add_after'
        with self.assertRaises(OSError):
            self.coordinator.tick(at=self.at)
        self.restart()
        self.coordinator.tick(at=self.at)
        self.assertEqual(self.native.starts, 1)
        self.assertEqual(sum(m == 'thread/queue/add' for m, _ in self.native.calls), 1)

    def test_lost_admission_without_evidence_is_uncertain_and_never_retried(self):
        self.ready_for_review()
        self.native.fail = 'add_before'
        with self.assertRaises(OSError):
            self.coordinator.tick(at=self.at)
        self.restart()
        self.coordinator.tick(at=self.at)
        self.coordinator.tick(at=self.at)
        self.assertEqual(self.coordinator.state['entries'][0]['stage'], 'uncertain')
        self.assertEqual(sum(m == 'thread/queue/add' for m, _ in self.native.calls), 1)
        self.assertEqual(self.native.starts, 0)

    def test_interrupted_start_with_effects_recovers_client_id_and_does_not_repeat_effects(self):
        self.ready_for_review()
        self.native.fail = 'start_after'
        with self.assertRaises(OSError):
            self.coordinator.tick(at=self.at)
        self.restart()
        self.coordinator.tick(at=self.at)
        self.assertEqual(self.coordinator.state['entries'][0]['stage'], 'observed_started')
        self.assertEqual(self.native.starts, 1)
        self.assertEqual(sum(m == 'thread/queue/start' for m, _ in self.native.calls), 1)

    def test_interrupted_start_without_terminal_proof_is_not_automatically_reissued(self):
        self.ready_for_review()
        self.native.fail = 'start_before'
        with self.assertRaises(OSError):
            self.coordinator.tick(at=self.at)
        self.restart()
        self.coordinator.tick(at=self.at)
        self.assertEqual(self.coordinator.state['entries'][0]['stage'], 'uncertain')
        self.assertEqual(self.native.starts, 0)
        self.assertEqual(len(self.native.pending), 1)
        self.assertEqual(sum(m == 'thread/queue/start' for m, _ in self.native.calls), 1)

    def test_native_client_consumption_before_explicit_start_is_observed(self):
        self.ready_for_review()
        self.native.fail = 'add_after'
        with self.assertRaises(OSError):
            self.coordinator.tick(at=self.at)
        e = self.coordinator.state['entries'][0]
        self.native.history = [{'turnId':'turn-fixture','item':{'type':'userMessage','id':'user-fixture','clientId':e['id']}}]
        self.native.pending = []
        self.native.status = 'active'
        self.restart()
        self.coordinator.tick(at=self.at)
        self.assertEqual(self.coordinator.state['entries'][0]['stage'], 'observed_started')
        self.assertFalse(any(m == 'thread/queue/start' for m, _ in self.native.calls))

    def test_wake_budget_deadline_and_batching_are_persistent(self):
        self.ready_for_review()
        h = self.coordinator.collect()
        h['queue'] = {'active': 1}
        self.config['min_ready'] = 2
        self.coordinator.tick(at=self.at, handoff=h)
        self.assertEqual(self.coordinator.state['status'], 'batching_results')
        self.coordinator.tick(at=self.at + timedelta(seconds=30), handoff=h)
        self.native.status = 'idle'
        h['keys'] = ['another-result']
        self.coordinator.tick(at=self.at + timedelta(seconds=31), handoff=h)
        self.assertEqual(self.coordinator.state['status'], 'batching_cooldown')
        self.coordinator.tick(at=self.at + timedelta(seconds=100), handoff=h)
        self.coordinator.tick(at=self.at + timedelta(seconds=140), handoff=h)
        self.restart()
        h['keys'] = ['third-result']
        self.coordinator.tick(at=self.at + timedelta(seconds=200), handoff=h)
        self.assertEqual(self.coordinator.state['status'], 'wake_budget_reached')
        self.coordinator.tick(at=self.at + timedelta(hours=2), handoff=h)
        self.assertEqual(self.coordinator.state['status'], 'deadline_reached')
        self.assertEqual(self.native.starts, 2)

    def test_explicit_budget_extension_preserves_spent_wakes_and_deadline(self):
        self.ready_for_review()
        handoff = self.coordinator.collect()
        self.config['max_wakes'] = 1
        self.coordinator.tick(at=self.at, handoff=handoff)
        spent = copy.deepcopy(self.coordinator.state['entries'])
        self.native.status = 'idle'
        handoff['keys'] = ['new-independent-result']
        self.coordinator.tick(at=self.at + timedelta(minutes=2), handoff=handoff)
        self.assertEqual(self.coordinator.state['status'], 'wake_budget_reached')
        self.config['max_wakes'] = 64  # Explicit new operator budget, never an automatic reset.
        self.restart()
        self.assertEqual(self.coordinator.state['entries'], spent)
        self.coordinator.tick(at=self.at + timedelta(minutes=3), handoff=handoff)
        self.assertEqual(self.native.starts, 2)
        self.assertEqual(self.coordinator.state['entries'][0], spent[0])
        self.assertEqual(len(self.coordinator.state['entries']), 2)
        self.coordinator.tick(at=self.at + timedelta(hours=2), handoff=handoff)
        self.assertEqual(self.coordinator.state['status'], 'deadline_reached')
        self.assertEqual(self.native.starts, 2)

    def test_long_window_wake_budget_validation_remains_explicit_and_finite(self):
        path = self.directory / 'config.json'
        for budget in (20, 64, 512):
            path.write_text(json.dumps(dict(self.config, max_wakes=budget)))
            path.chmod(0o600)
            self.assertEqual(load_config(path)['max_wakes'], budget)
        for budget in (0, 513, True):
            path.write_text(json.dumps(dict(self.config, max_wakes=budget)))
            with self.assertRaisesRegex(DispatchError, 'Invalid coordinator max_wakes'):
                load_config(path)

    def test_uncapped_review_configuration_retains_flood_guards(self):
        path = self.directory / 'config.json'
        for fields in ({'max_wakes': None}, {'max_escalations': None},
                       {'max_wakes': None, 'max_escalations': None}):
            path.write_text(json.dumps(dict(self.config, **fields)))
            path.chmod(0o600)
            actual = load_config(path)
            for key, value in fields.items():
                self.assertIs(actual[key], value)
        for fields in ({'cooldown': 0}, {'urgent_cooldown': 15}):
            path.write_text(json.dumps(dict(self.config, max_wakes=None, **fields)))
            with self.assertRaisesRegex(DispatchError, 'Uncapped review'):
                load_config(path)

    def test_uncapped_wakes_keep_history_cooldown_deduplication_and_deadline(self):
        self.ready_for_review()
        self.config['max_wakes'] = None
        handoff = self.coordinator.collect()
        self.coordinator.tick(at=self.at, handoff=handoff)
        first = copy.deepcopy(self.coordinator.state['entries'][0])
        self.native.status = 'idle'
        handoff['keys'] = ['next-independent-result']
        self.coordinator.tick(at=self.at + timedelta(seconds=1), handoff=handoff)
        self.assertEqual(self.coordinator.state['status'], 'batching_cooldown')
        for index in range(1, 5):
            self.native.status = 'idle'
            handoff['keys'] = ['independent-result-' + str(index)]
            self.coordinator.tick(at=self.at + timedelta(seconds=61 * index), handoff=handoff)
        self.restart()
        self.assertEqual(len(self.coordinator.state['entries']), 5)
        self.assertEqual(self.coordinator.state['entries'][0], first)
        self.native.status = 'idle'
        self.coordinator.tick(at=self.at + timedelta(seconds=400), handoff=handoff)
        self.assertEqual(self.coordinator.state['status'], 'wake_already_observed')
        handoff['keys'] = ['outside-window']
        self.coordinator.tick(at=self.at + timedelta(hours=2), handoff=handoff)
        self.assertEqual(self.coordinator.state['status'], 'deadline_reached')
        self.assertEqual(self.native.starts, 5)

    def test_uncapped_escalations_are_per_issue_and_rate_limited(self):
        self.git('branch', 'target')
        self.config.update(allow_effort_override=True, max_wakes=None,
                           max_escalations=None, urgent_cooldown=60)
        self.restart()
        with patch.dict(os.environ, CODEX_THREAD_ID='thread-fixture'):
            with self.assertRaisesRegex(DispatchError, 'stable --issue'):
                self.coordinator.request_escalation('failed_tests', 'reason')
            self.coordinator.request_escalation('failed_tests', 'reason', 'issue-0')
            self.coordinator.tick(at=self.at)
            self.native.status = 'idle'
            self.coordinator.request_escalation('failed_tests', 'new issue', 'issue-1')
            self.coordinator.tick(at=self.at + timedelta(seconds=1))
            self.assertEqual(self.coordinator.state['status'], 'batching_cooldown')
            self.coordinator.tick(at=self.at + timedelta(seconds=61))
            for index in range(2, 5):
                self.native.status = 'idle'
                self.coordinator.request_escalation('failed_tests', 'new issue', 'issue-' + str(index))
                self.coordinator.tick(at=self.at + timedelta(seconds=61 * index))
            with self.assertRaisesRegex(DispatchError, 'already escalated'):
                self.coordinator.request_escalation('failed_tests', 'retry old issue', 'issue-0')
        self.assertEqual(self.native.starts, 5)
        self.assertEqual(len(self.coordinator.state['entries']), 5)

    def test_unfinished_review_has_one_durable_reconciliation_without_task_replay(self):
        base,commit=self.ready_for_review()
        self.config.update(max_wakes=6,max_unfinished_reconciliations=1)
        self.restart();self.coordinator.tick(at=self.at)
        original=copy.deepcopy(self.coordinator.state['entries'][0])
        posts=len(self.api.posts)
        self.coordinator.tick(at=self.at+timedelta(minutes=2))
        self.assertEqual(self.native.starts,1)  # Original reviewer still active.
        self.native.status='idle';self.restart()
        self.coordinator.tick(at=self.at+timedelta(minutes=3))
        self.assertEqual(self.native.starts,2)
        self.assertEqual(len(self.api.posts),posts)  # Never redispatch the OpenCode task.
        self.assertEqual(self.coordinator.state['entries'][0],original)
        self.assertEqual(len(self.coordinator.state['entries'][1]['reconciled_units']),1)
        self.native.status='idle';self.restart()
        self.coordinator.tick(at=self.at+timedelta(minutes=5))
        self.assertEqual(self.native.starts,2)  # Finite, not an unchanged-work loop.
        self.git('update-ref','refs/heads/target',commit,base)
        self.assertEqual(self.coordinator.collect()['unfinished_units'],[])

    def test_unfinished_review_respects_independent_holds_not_advisory_verdicts(self):
        base,commit=self.ready_for_review()
        self.config.update(max_wakes=6,max_unfinished_reconciliations=1)
        self.restart();self.coordinator.tick(at=self.at)
        root=self.root/'shadow-recovery';root.mkdir(mode=0o700)
        config=root/'policy.json'
        config.write_text(json.dumps({'version':1,'root':str(root),'database':str(self.ledger),
            'repository':str(self.repo),'target_ref':'target','max_cases':1,'max_disagreements':1}))
        config.chmod(0o600);self.config['shadow_policy']=str(config);self.restart()
        path=root/'case-fixture.json';case={'id':'fixture','source_task':'doc-1','source_commit':commit,
            'input_hash':'a'*64,'result':{'assessment':'blocked','issues':[],'escalation':[]}}
        path.write_text(json.dumps(case))
        self.assertEqual(len(self.coordinator.collect()['unfinished_units']),1)
        case['reference']={'assessment':'blocked','input_hash':'a'*64};path.write_text(json.dumps(case))
        self.assertEqual(self.coordinator.collect()['unfinished_units'],[])
        case['source_commit']='f'*40;path.write_text(json.dumps(case))
        self.assertEqual(len(self.coordinator.collect()['unfinished_units']),1)

    def test_unfinished_review_keeps_wake_budget_and_deadline(self):
        self.ready_for_review();self.config.update(max_wakes=1,max_unfinished_reconciliations=1)
        self.restart();self.coordinator.tick(at=self.at);self.native.status='idle'
        self.coordinator.tick(at=self.at+timedelta(minutes=3))
        self.assertEqual(self.coordinator.state['status'],'wake_budget_reached')
        self.assertEqual(self.native.starts,1)
        self.coordinator.tick(at=self.at+timedelta(hours=2))
        self.assertEqual(self.coordinator.state['status'],'deadline_reached')

    def test_unfinished_review_reconciliation_bound_is_explicit(self):
        path=self.directory/'config.json'
        for count in (0,1,2):
            path.write_text(json.dumps(dict(self.config,max_unfinished_reconciliations=count)))
            path.chmod(0o600)
            self.assertEqual(load_config(path)['max_unfinished_reconciliations'],count)
        for count in (-1,3,True):
            path.write_text(json.dumps(dict(self.config,max_unfinished_reconciliations=count)))
            with self.assertRaisesRegex(DispatchError,'Invalid unfinished-review'):
                load_config(path)

    def test_manual_historical_closeouts_do_not_trigger_recurring_notifications(self):
        self.git('branch', 'target')
        self.enqueue()
        self.batch.tick(self.at)
        self.api.finish(terminal=None)
        self.api.append('ses_fixture', 'session.execution.failed')
        self.batch.tick(self.at)
        self.dispatcher.update('doc-1', state='accounted')
        self.life.save('doc-1', 'ready', learning='manual_closeout', final_commit=self.base)
        self.batch.save('doc-1', 'deferred', 'Explicit historical closeout')
        self.coordinator.tick(at=self.at)
        self.assertEqual(self.coordinator.state['status'], 'no_new_review_work')
        self.assertEqual(self.native.calls, [])

    def test_notifier_failure_does_not_block_independent_queued_work(self):
        self.ready_for_review()
        self.native.fail = 'start_before'
        with self.assertRaises(OSError):
            self.coordinator.tick(at=self.at)
        repo = self.second_session()
        self.batch.enqueue('doc-2', 'second', 'Change pending to verified.', ['README.md'], self.policy)
        self.configure(state='available')
        self.batch.tick(self.at)
        self.assertEqual(self.dispatcher.task('doc-2')['state'], 'admitted')
        self.assertEqual(len(self.api.posts), 2)

    def test_already_consumed_stable_id_is_not_admitted_again_after_receipt_loss(self):
        self.ready_for_review()
        self.coordinator.tick(at=self.at)
        self.coordinator.state['seen'] = []  # Lost dedup metadata; native history is still authoritative.
        self.coordinator.state['entries'] = []
        self.native.status = 'idle'
        self.coordinator.tick(at=self.at + timedelta(minutes=5))
        self.assertEqual(self.native.starts, 1)
        self.assertEqual(sum(m == 'thread/queue/add' for m, _ in self.native.calls), 1)
        self.assertEqual(self.coordinator.state['status'], 'wake_already_observed')

    def test_effort_policy_changes_only_future_coordinator_effort_and_escalates_once(self):
        self.ready_for_review()
        self.config['allow_effort_override'] = True
        self.config['base_effort'] = 'medium'
        self.restart()
        self.coordinator.tick(at=self.at)
        effort = [p for m,p in self.native.calls if m == 'thread/settings/update']
        self.assertEqual(effort, [{'threadId':'thread-fixture','effort':'medium'}])
        request = self.directory / 'coordinator-escalation.json'
        request.write_text(json.dumps({'id':'risk-1','thread_id':'thread-fixture','risk':'memory_safety','reason':'specific unresolved source invariant'}))
        request.chmod(0o600)
        self.native.status = 'idle'
        self.coordinator.tick(at=self.at + timedelta(seconds=1))
        effort = [p for m,p in self.native.calls if m == 'thread/settings/update']
        self.assertEqual(effort[-1], {'threadId':'thread-fixture','effort':'xhigh'})
        self.assertTrue(self.coordinator.state['entries'][-1]['escalation'])
        self.assertEqual(self.native.starts, 2)

    def test_low_backlog_wakes_without_completed_units_and_deduplicates_across_restart(self):
        self.git('branch', 'target')
        self.enqueue()
        self.config.update(backlog_target=2, refill_pools=['gpu'])
        before = self.ledger.read_bytes(), self.git('show-ref'), list(self.api.posts)
        self.coordinator.tick(at=self.at)
        entry = self.coordinator.state['entries'][0]
        handoff = json.loads(Path(entry['handoff']).read_text())
        self.assertEqual(handoff['units'], [])
        self.assertEqual(handoff['replenishment'][0]['independent_pending'], 1)
        self.assertEqual(self.native.starts, 1)
        self.assertEqual((self.ledger.read_bytes(), self.git('show-ref'), list(self.api.posts)), before)
        self.native.status = 'idle'
        self.restart()
        self.coordinator.tick(at=self.at + timedelta(minutes=5))
        self.assertEqual(self.native.starts, 1)
        self.assertEqual(self.coordinator.state['status'], 'no_new_review_work')

    def test_refill_requires_explicit_open_observed_pool_and_independent_shortfall(self):
        self.config.update(backlog_target=1, refill_pools=['gpu'])
        pool = {'name':'gpu','until_at':self.until,'admission_gate':'open',
                'occupancy':'observed','available_slots':0}
        task = {'task_id':'queued','task_pool':'gpu','queue_state':'pending','dependencies':[]}
        snapshot = {'pools':[pool], 'tasks':[task]}
        self.assertEqual(self.coordinator.replenishment(snapshot), [])
        task['dependencies'] = [{'task':'future','when':'integrated'}]
        pressure = self.coordinator.replenishment(snapshot)
        self.assertEqual(pressure[0]['independent_pending'], 0)
        for key, value in [('admission_gate','paused'),('admission_gate','outside_window'),
                           ('occupancy','unknown'),('name','unapproved')]:
            changed = copy.deepcopy(snapshot)
            changed['pools'][0][key] = value
            self.assertEqual(self.coordinator.replenishment(changed), [])

    def test_shadow_jobs_do_not_pad_backlog_or_generate_refill_progress(self):
        self.config.update(backlog_target=2,refill_pools=['gpu'])
        snapshot={'pools':[{'name':'gpu','until_at':self.until,'admission_gate':'open',
                           'occupancy':'observed','available_slots':1}],
                  'tasks':[{'task_id':'shadow-job','task_pool':'gpu','queue_state':'pending',
                            'dependencies':[],'purpose':'shadow_review'}]}
        value=self.coordinator.replenishment(snapshot)[0]
        self.assertEqual(value['independent_pending'],0)
        snapshot['tasks'][0]['queue_state']='ready'
        self.assertEqual(self.coordinator.replenishment(snapshot)[0]['key'],value['key'])

    def test_shadow_judgment_is_hidden_until_reference_is_frozen(self):
        self.ready_for_review()
        root=self.root/'shadows';root.mkdir(mode=0o700)
        path=root/'policy.json'
        path.write_text(json.dumps({'version':1,'max_cases':10,'max_disagreements':3,
                                   'root':str(root),'database':str(self.ledger),'repository':str(self.repo)}))
        path.chmod(0o600);self.config['shadow_policy']=str(path)
        case={'id':'case1','source_task':'doc-1','input_hash':'frozen','kind':'forward','stage':'complete',
              'result':{'assessment':'blocked','issues':['PRIVATE SHADOW OPINION'],'escalation':[]}}
        (root/'case-case1.json').write_text(json.dumps(case))
        self.assertNotIn('PRIVATE SHADOW OPINION',json.dumps(self.coordinator.collect()))
        case.update(reference={'assessment':'routine'},disagreement=True,result_commit='frozen-result')
        (root/'case-case1.json').write_text(json.dumps(case))
        self.assertTrue(self.coordinator.collect()['shadow_cases'][0]['disagreement'])

    def test_refill_fingerprint_ignores_cursor_learning_and_clock_but_tracks_real_progress(self):
        self.config.update(backlog_target=2, refill_pools=['gpu'])
        snapshot = {'pools':[{'name':'gpu','until_at':self.until,'admission_gate':'open',
                             'occupancy':'observed','available_slots':0}],
                    'tasks':[{'task_id':'a','task_pool':'gpu','queue_state':'active',
                              'dependencies':[],'stage':'task','event_cursor':1}]}
        first = self.coordinator.replenishment(snapshot)[0]['key']
        snapshot['generated_at'] = 'later'
        snapshot['tasks'][0].update(stage='learning', event_cursor=200)
        self.assertEqual(self.coordinator.replenishment(snapshot)[0]['key'], first)
        snapshot['tasks'][0]['queue_state'] = 'ready'
        self.assertNotEqual(self.coordinator.replenishment(snapshot)[0]['key'], first)

    def test_ready_integration_prerequisite_bypasses_batch_delay_but_respects_urgent_cooldown(self):
        self.ready_for_review()
        self.second_session()
        self.batch.enqueue('dependent', 'second', 'Review README.', ['README.md'], self.policy,
                           dependencies=[{'task':'doc-1','when':'integrated','ref':'refs/heads/target'}])
        self.config.update(min_ready=5, max_wait=3600, cooldown=600, urgent_cooldown=15)
        handoff = self.coordinator.collect()
        self.assertEqual(handoff['dependency_ready'], ['doc-1'])
        self.coordinator.tick(at=self.at, handoff=handoff)
        self.assertEqual(self.native.starts, 1)
        self.native.status = 'idle'
        handoff['keys'] = ['new-result']
        self.coordinator.tick(at=self.at + timedelta(seconds=10), handoff=handoff)
        self.assertEqual(self.coordinator.state['status'], 'batching_cooldown')
        self.coordinator.tick(at=self.at + timedelta(seconds=15), handoff=handoff)
        self.assertEqual(self.native.starts, 2)

    def test_refill_keeps_wake_budget_human_queue_and_deadline_guards(self):
        self.git('branch', 'target')
        self.config.update(backlog_target=3, refill_pools=['gpu'], max_wakes=1)
        self.native.pending = [{'id':'human','clientUserMessageId':'human','input':[]}]
        self.coordinator.tick(at=self.at)
        self.assertEqual(self.native.starts, 0)
        self.native.pending = []
        self.coordinator.tick(at=self.at)
        self.assertEqual(self.native.starts, 1)
        self.native.status = 'idle'
        # Real new queue work can require another top-up, never another budget.
        self.enqueue()
        self.coordinator.tick(at=self.at + timedelta(minutes=5))
        self.assertEqual(self.coordinator.state['status'], 'wake_budget_reached')
        self.coordinator.tick(at=self.at + timedelta(hours=2))
        self.assertEqual(self.coordinator.state['status'], 'deadline_reached')

    def test_distinct_issues_get_bounded_escalations_without_replaying_one_issue(self):
        self.git('branch', 'target')
        self.config.update(allow_effort_override=True, base_effort='medium', max_wakes=5, max_escalations=2)
        self.restart()
        with patch.dict(os.environ, CODEX_THREAD_ID='thread-fixture'):
            first = self.coordinator.request_escalation('memory_safety', 'specific invariant', 'task-a')
            self.coordinator.tick(at=self.at)
            self.assertTrue(self.coordinator.state['entries'][-1]['escalation'])
            self.assertEqual(self.coordinator.state['entries'][-1]['escalation_issue'], 'task-a')
            with self.assertRaisesRegex(DispatchError, 'already escalated'):
                self.coordinator.request_escalation('memory_safety', 'retry', 'task-a')
            second = self.coordinator.request_escalation('conflicting_evidence', 'another unit', 'task-b')
            self.assertNotEqual(first['id'], second['id'])
            self.assertEqual(json.loads((self.directory / ('coordinator-escalation-' + first['id'] + '.json')).read_text()), first)
            self.native.status = 'idle'
            self.coordinator.tick(at=self.at + timedelta(seconds=1))
            self.assertEqual(self.native.starts, 2)
            with self.assertRaisesRegex(DispatchError, 'escalation budget'):
                self.coordinator.request_escalation('source_ambiguity', 'third', 'task-c')

    def test_pending_escalation_intent_is_not_replaced_and_caller_is_fenced(self):
        self.config.update(allow_effort_override=True, max_wakes=5, max_escalations=3)
        self.restart()
        with patch.dict(os.environ, CODEX_THREAD_ID='wrong-thread'):
            with self.assertRaisesRegex(DispatchError, 'configured coordinator'):
                self.coordinator.request_escalation('failed_tests', 'reason', 'task-a')
        with patch.dict(os.environ, CODEX_THREAD_ID='thread-fixture'):
            with self.assertRaisesRegex(DispatchError, 'stable --issue'):
                self.coordinator.request_escalation('failed_tests', 'reason')
            self.coordinator.request_escalation('failed_tests', 'reason', 'task-a')
            pending = (self.directory / 'coordinator-escalation.json').read_bytes()
            with self.assertRaisesRegex(DispatchError, 'still unresolved'):
                self.coordinator.request_escalation('failed_tests', 'other', 'task-b')
            self.assertEqual((self.directory / 'coordinator-escalation.json').read_bytes(), pending)

    def test_legacy_escalation_counts_toward_new_explicit_budget(self):
        self.config.update(allow_effort_override=True, max_wakes=5)
        self.restart()
        self.coordinator.state['entries'] = [{'escalation':True}]
        with patch.dict(os.environ, CODEX_THREAD_ID='thread-fixture'):
            with self.assertRaisesRegex(DispatchError, 'escalation budget'):
                self.coordinator.request_escalation('failed_tests', 'new issue', 'task-b')
            self.config['max_escalations'] = 2
            self.coordinator.request_escalation('failed_tests', 'new issue', 'task-b')

    def test_interrupted_escalation_pointer_write_retains_intent_without_another_request(self):
        from runner import write_json
        self.config.update(allow_effort_override=True, max_wakes=5, max_escalations=3)
        self.restart()
        def interrupted(path, value):
            if path.name == 'coordinator-escalation.json':
                raise OSError('Interrupted pointer write')
            write_json(path, value)
        with patch.dict(os.environ, CODEX_THREAD_ID='thread-fixture'):
            with patch('coordinator.write_json', side_effect=interrupted), self.assertRaises(OSError):
                self.coordinator.request_escalation('failed_tests', 'reason', 'task-a')
            self.assertEqual(len(list(self.directory.glob('coordinator-escalation-*.json'))), 1)
            with self.assertRaisesRegex(DispatchError, 'already has an escalation intent'):
                self.coordinator.request_escalation('failed_tests', 'reason', 'task-a')
        self.assertEqual(self.native.calls, [])

    def test_optional_policy_validation_preserves_legacy_defaults_and_bounds(self):
        path = self.directory / 'config.json'
        for settings in ({}, {'backlog_target':-1}, {'backlog_target':2},
                         {'refill_pools':[{}]}, {'max_escalations':3},
                         {'backlog_target':2,'refill_pools':['gpu'],'urgent_cooldown':15,'max_escalations':2}):
            value = dict(self.config, **settings)
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            if not settings or 'urgent_cooldown' in settings:
                self.assertEqual(load_config(path)['thread_id'], 'thread-fixture')
            else:
                with self.assertRaises(DispatchError):
                    load_config(path)

    def test_parallel_escalation_writers_cannot_replace_the_pending_pointer(self):
        self.config.update(allow_effort_override=True, max_wakes=5, max_escalations=3)
        self.restart()
        with patch.dict(os.environ, CODEX_THREAD_ID='thread-fixture'):
            with lock(self.directory / 'escalation'), self.assertRaises(DispatchError):
                self.coordinator.request_escalation('failed_tests', 'reason', 'task-a')
        self.assertFalse((self.directory / 'coordinator-escalation.json').exists())

    def test_foreground_notifier_can_stop_without_touching_agents_and_rejects_duplicate_owner(self):
        self.git('branch', 'target')
        config = self.directory / 'config.json'
        config.write_text(json.dumps(self.config))
        config.chmod(0o600)
        script = str(Path(__file__).with_name('coordinator.py'))
        child = subprocess.Popen([sys.executable, script, '--config', str(config), 'run'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            until = time.monotonic() + 5
            while time.monotonic() < until:
                if self.coordinator.path.exists() and json.loads(self.coordinator.path.read_text()).get('owner'):
                    break
                time.sleep(0.05)
            state = json.loads(self.coordinator.path.read_text())
            self.assertEqual(state['owner']['pid'], child.pid)
            with self.assertRaisesRegex(DispatchError, 'Another coordinator'):
                with lock(self.ledger):
                    pass
            result = subprocess.run([sys.executable, script, '--config', str(config), 'stop'], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(child.wait(timeout=7), 0)
            self.assertEqual(self.api.posts, [])
        finally:
            if child.poll() is None:
                child.terminate()  # Only this test's owned notifier, not any agent/server.
                child.wait(timeout=5)
            child.stderr.close()

    def test_lock_prevents_duplicate_notifiers_and_configuration_drift(self):
        with lock(self.ledger), self.assertRaisesRegex(DispatchError, 'Another coordinator'):
            with lock(self.ledger):
                pass
        self.coordinator.save()
        config = dict(self.config, thread_id='another-thread')
        with self.assertRaisesRegex(DispatchError, 'configuration drift'):
            Coordinator(config)


class BridgeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('bun'), 'Optional notifier bridge requires installed Bun')
    def test_bun_websocket_unix_transport_roundtrip_in_a_disposable_server(self):
        import tempfile
        import selectors
        with tempfile.TemporaryDirectory(prefix='codex-rpc-fixture-') as tmp:
            root = Path(tmp)
            script = root / 'server.js'
            path = root / 'actual.sock'
            link = root / 'control.sock'
            link.symlink_to(path)
            script.write_text('''import {chmodSync} from 'node:fs';
const server=Bun.serve({unix:process.argv[2],fetch(req,server){if(server.upgrade(req))return;return new Response('upgrade',{status:400});},websocket:{message(ws,data){const m=JSON.parse(data);if(m.id)ws.send(JSON.stringify({id:m.id,result:m.method==='initialize'?{}:{thread:{cwd:'/fixture',status:{type:'idle'}}}}));}}});
chmodSync(process.argv[2],0o777);console.log('ready');
''')
            p = subprocess.Popen([shutil.which('bun'), str(script), str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                with selectors.DefaultSelector() as sel:
                    sel.register(p.stdout, selectors.EVENT_READ)
                    self.assertTrue(sel.select(5))
                    self.assertEqual(p.stdout.readline().strip(), b'ready')
                client = CodexRPC({'codex_socket': str(link), 'bun': shutil.which('bun')})
                try:
                    self.assertEqual(client.call('thread/read', {'threadId': 'fixture'})['thread']['status'], {'type':'idle'})
                finally:
                    client.close()
            finally:
                p.terminate()
                p.wait(timeout=5)
                p.stdout.close()
                p.stderr.close()


if __name__ == '__main__':
    unittest.main()
