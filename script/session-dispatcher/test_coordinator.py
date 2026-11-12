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
