"""Disposable Git + loopback HTTP contract fixtures; no real model or session."""
import copy
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.parse

from dispatch import Client, Dispatcher, DispatchError, encoded


class API:
    def __init__(self, root):
        self.sessions = {}
        self.posts = []
        self.cut_after = None
        self.lose_receipt = False
        self.retain = True
        self.redirect = False
        self.captured = 0
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                parts = url.path.strip('/').split('/')
                if fixture.redirect:
                    self.send_response(302)
                    self.send_header('Location', fixture.url + '/capture')
                    self.end_headers()
                    return
                if url.path == '/capture':
                    fixture.captured += 1
                if url.path == '/api/session/active':
                    return self.json({'data': {key: {'type': 'running'} for key, value in fixture.sessions.items() if value['active']}})
                sid = parts[3] if parts[1] == 'experimental' else parts[2]
                item = fixture.sessions[sid]
                if len(parts) == 3:
                    return self.json({'data': item['info']})
                if parts[-1] in ('inbox', 'form'):
                    return self.json({'data': list(item['inbox'].values()) if parts[-1] == 'inbox' else item['forms']})
                query = urllib.parse.parse_qs(url.query)
                after = int(query.get('after', ['-1'])[0])
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                events = [event for event in item['events'] if event['durable']['seq'] > after] if fixture.retain else []
                for index, event in enumerate(events):
                    if fixture.cut_after is not None and index >= fixture.cut_after:
                        self.wfile.write(b'data: {"type":')
                        self.wfile.flush()
                        return
                    self.wfile.write(('data: ' + encoded(event) + '\n\n').encode())
                self.wfile.write(('data: ' + encoded({'type': 'log.synced', 'aggregateID': sid,
                                                    'seq': item['events'][-1]['durable']['seq']}) + '\n\n').encode())

            def do_POST(self):
                sid = self.path.split('/')[3]
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.posts.append((sid, payload))
                item = fixture.sessions[sid]
                if payload['id'] not in item['admissions']:
                    admission = {'id': payload['id'], 'sessionID': sid, 'type': 'user', 'time': {'created': 1},
                                 'delivery': payload['delivery'],
                                 'payload': {key: payload[key] for key in ('text', 'metadata')}}
                    item['admissions'][payload['id']] = admission
                    item['inbox'][payload['id']] = admission
                    fixture.append(sid, 'session.inbox.enqueued', inboxID=payload['id'],
                                   item={'type': 'user', 'payload': admission['payload'], 'delivery': payload['delivery']})
                if fixture.lose_receipt:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                receipt = copy.deepcopy(item['admissions'][payload['id']])
                receipt['time']['created'] += len(fixture.posts)  # Pilot: response timestamps are not stable identity.
                self.json({'data': receipt})

            def json(self, value):
                body = encoded(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.connection = root / 'connection.json'
        self.connection.write_text(encoded({'url': self.url, 'password': 'disposable-fixture'}))
        self.connection.chmod(0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def session(self, worktree, sid='ses_fixture'):
        self.sessions[sid] = {'info': {'id': sid, 'projectID': 'project_fixture', 'agent': 'build',
                                     'model': {'providerID': 'fixture', 'id': 'fixture'},
                                     'location': {'directory': str(worktree)}},
                              'active': False, 'forms': [], 'inbox': {}, 'admissions': {}, 'events': []}
        self.append(sid, 'session.created')
        return sid

    def append(self, sid, kind, **data):
        events = self.sessions[sid]['events']
        seq = len(events)
        event = {'id': f'evt_{sid}_{seq}', 'created': seq, 'type': kind,
                 'durable': {'aggregateID': sid, 'seq': seq, 'version': 1}, 'data': {'sessionID': sid, **data}}
        events.append(event)
        return event

    def finish(self, sid='ses_fixture', terminal='succeeded'):
        self.append(sid, 'session.execution.started')
        item = self.sessions[sid]
        for inbox_id in list(item['inbox']):
            self.append(sid, 'session.inbox.delivered', inboxID=inbox_id)
            del item['inbox'][inbox_id]
        if terminal:
            self.append(sid, 'session.execution.' + terminal)
        item['active'] = False


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dispatch-test-')
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'fixture')
        self.git('config', 'user.name', 'Dispatcher Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        (self.repo / 'README.md').write_text('Task status: pending.\n')
        self.git('add', 'README.md')
        self.git('commit', '-m', 'fixture base')
        self.base = self.git('rev-parse', 'HEAD')
        self.api = API(self.root)
        self.api.session(self.repo)
        self.ledger = self.root / 'private' / 'ledger.sqlite3'
        self.dispatcher = Dispatcher(self.ledger)
        self.approve()

    def tearDown(self):
        self.dispatcher.close()
        self.api.close()
        self.temp.cleanup()

    def git(self, *args, path=None):
        return subprocess.check_output(['git', '-C', str(path or self.repo), *args],
                                       env={**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'},
                                       stderr=subprocess.DEVNULL).decode().strip()

    def approve(self, name='worker', sid='ses_fixture', repo=None):
        return self.dispatcher.approve(name, sid, repo or self.repo, self.api.connection)

    def submit(self, task='doc-1', worker='worker', text='Change pending to verified.'):
        return self.dispatcher.submit(task, worker, text, ['README.md'])

    def commit(self, task='doc-1', extra=False, trailer=True):
        (self.repo / 'README.md').write_text('Task status: verified.\n')
        if extra:
            (self.repo / 'outside.txt').write_text('outside scope\n')
        self.git('add', '.')
        self.git('commit', '-m', 'docs: fixture result' + (f'\n\nOpenCode-Task: {task}' if trailer else ''))
        return self.git('rev-parse', 'HEAD')

    def test_same_task_is_idempotent_across_client_restart_without_second_post(self):
        first = self.submit()
        self.dispatcher.close()
        self.dispatcher = Dispatcher(self.ledger)
        second = self.submit()
        self.assertEqual(first['message_id'], second['message_id'])
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.api.posts[0][1]['delivery'], 'queue')
        self.assertTrue(self.api.posts[0][1]['resume'])
        with self.assertRaisesRegex(DispatchError, 'different assignment'):
            self.submit(text='Different assignment')

    def test_duplicate_assignment_same_session_is_rejected(self):
        self.submit()
        with self.assertRaisesRegex(DispatchError, 'unresolved'):
            self.submit('doc-2')
        self.assertEqual(len(self.api.posts), 1)

    def test_concurrent_dispatchers_admit_only_one_assignment(self):
        barrier = threading.Barrier(2)

        class RacingClient(Client):
            def tail(self, session_id):
                tail = super().tail(session_id)
                barrier.wait(timeout=5)
                return tail

        results = []

        def submit(task_id):
            dispatcher = Dispatcher(self.ledger, client_factory=RacingClient)
            try:
                results.append(dispatcher.submit(task_id, 'worker', 'Change pending to verified.', ['README.md']))
            except DispatchError as error:
                results.append(error)
            finally:
                dispatcher.close()

        threads = [threading.Thread(target=submit, args=(task_id,)) for task_id in ('doc-a', 'doc-b')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(isinstance(result, DispatchError) for result in results), 1)
        self.assertEqual(len(self.api.posts), 1)

    def test_two_sessions_cannot_own_one_worktree(self):
        self.api.session(self.repo, 'ses_second')
        self.approve('second', 'ses_second')
        self.submit()
        with self.assertRaisesRegex(DispatchError, 'unresolved'):
            self.submit('doc-2', 'second')
        self.assertEqual(len(self.api.posts), 1)

    def test_same_branch_in_two_forced_disposable_worktrees_is_reserved_once(self):
        twin = self.root / 'twin'
        self.git('worktree', 'add', '--force', str(twin), 'fixture')
        self.api.session(twin, 'ses_twin')
        self.approve('twin', 'ses_twin', twin)
        self.submit()
        with self.assertRaisesRegex(DispatchError, 'unresolved'):
            self.submit('doc-2', 'twin')
        self.assertEqual(len(self.api.posts), 1)

    def test_busy_session_and_foreign_pending_input_refuse_submission(self):
        self.api.sessions['ses_fixture']['active'] = True
        with self.assertRaisesRegex(DispatchError, 'busy'):
            self.submit()
        self.api.sessions['ses_fixture']['active'] = False
        self.api.sessions['ses_fixture']['forms'] = [{'id': 'form_fixture'}]
        with self.assertRaisesRegex(DispatchError, 'forms'):
            self.submit()
        self.assertEqual(self.api.posts, [])

    def test_head_dirty_and_context_drift_refuse_before_post(self):
        (self.repo / 'README.md').write_text('uncommitted\n')
        with self.assertRaisesRegex(DispatchError, 'drift'):
            self.submit()
        self.commit('unrelated')
        with self.assertRaisesRegex(DispatchError, 'drift'):
            self.submit()
        self.approve()
        self.api.append('ses_fixture', 'session.renamed')
        with self.assertRaisesRegex(DispatchError, 'Session changed'):
            self.submit()
        self.assertEqual(self.api.posts, [])

    def test_branch_drift_after_send_is_uncertain(self):
        self.submit()
        self.git('branch', '-m', 'changed')
        state = self.dispatcher.reconcile('doc-1')
        self.assertEqual(state['state'], 'uncertain')
        self.assertIn('drift', state['hazard'])

    def test_interrupted_observation_saves_cursor_and_reconnects_without_resending(self):
        self.submit()
        self.api.finish()
        self.api.cut_after = 2
        partial = self.dispatcher.reconcile('doc-1')
        self.assertEqual(partial['state'], 'uncertain')
        self.assertEqual(partial['cursor'], 2)
        self.assertIsNone(partial['hazard'])
        self.dispatcher.close()
        self.dispatcher = Dispatcher(self.ledger)
        self.api.cut_after = None
        complete = self.dispatcher.reconcile('doc-1')
        self.assertEqual(complete['cursor'], 4)
        self.assertEqual(complete['state'], 'completed')
        self.assertEqual(self.dispatcher.db.execute('SELECT COUNT(*) FROM observed').fetchone()[0], 4)
        self.dispatcher.reconcile('doc-1')
        self.assertEqual(self.dispatcher.db.execute('SELECT COUNT(*) FROM observed').fetchone()[0], 4)
        self.assertEqual(len(self.api.posts), 1)

    def test_lost_admission_response_is_reconciled_without_retry(self):
        self.api.lose_receipt = True
        first = self.submit()
        self.assertEqual(first['state'], 'uncertain')
        self.submit()
        self.assertEqual(len(self.api.posts), 1)
        self.api.finish()
        self.assertEqual(self.dispatcher.reconcile('doc-1')['state'], 'completed')
        self.assertEqual(len(self.api.posts), 1)

    def test_no_retained_tail_refuses_approval_and_gap_refuses_completion(self):
        self.api.retain = False
        with self.assertRaisesRegex(DispatchError, 'not retained'):
            self.approve()
        self.api.retain = True
        self.submit()
        self.api.finish()
        self.api.retain = False
        state = self.dispatcher.reconcile('doc-1')
        self.assertEqual(state['state'], 'uncertain')
        self.assertIn('Missing replay', state['hazard'])
        self.assertEqual(state['cursor'], 0)

    def test_terminal_success_is_not_git_proof(self):
        self.submit()
        self.api.finish()
        self.assertEqual(self.dispatcher.reconcile('doc-1')['state'], 'completed')
        with self.assertRaisesRegex(DispatchError, 'one normal commit'):
            self.dispatcher.verify('doc-1', self.base)
        self.assertIsNone(self.dispatcher.status('doc-1')['verification'])

    def test_commit_before_failed_or_missing_terminal_remains_uncertain(self):
        self.submit()
        commit = self.commit()
        self.api.finish(terminal='failed')
        state = self.dispatcher.verify('doc-1', commit)
        self.assertEqual(state['state'], 'uncertain')
        self.assertEqual(state['verification']['commit'], commit)
        self.assertEqual(state['terminal'], 'failed')
        with self.assertRaisesRegex(DispatchError, 'unresolved'):
            self.submit('doc-2')
        self.assertEqual(len(self.api.posts), 1)

    def test_actual_commit_without_terminal_does_not_free_assignment(self):
        self.submit()
        commit = self.commit()
        self.api.finish(terminal=None)
        state = self.dispatcher.verify('doc-1', commit)
        self.assertEqual(state['state'], 'uncertain')
        self.assertEqual(state['verification']['kind'], 'git_scope')
        self.assertIsNone(state['terminal'])
        self.assertEqual(self.dispatcher.result('doc-1')['verification']['commit'], commit)

    def test_late_terminal_does_not_automatically_verify_stale_git_proof(self):
        self.submit()
        commit = self.commit()
        self.api.finish(terminal=None)
        self.assertEqual(self.dispatcher.verify('doc-1', commit)['state'], 'uncertain')
        (self.repo / 'README.md').write_text('later uncommitted effects\n')
        self.api.append('ses_fixture', 'session.execution.succeeded')
        self.assertEqual(self.dispatcher.reconcile('doc-1')['state'], 'completed')
        with self.assertRaisesRegex(DispatchError, 'clean worktree'):
            self.dispatcher.verify('doc-1', commit)

    def test_interrupted_execution_and_restart_are_not_a_duplicate_admission_guarantee(self):
        self.submit()
        self.api.finish(terminal='interrupted')
        self.api.append('ses_fixture', 'session.execution.started')
        self.api.append('ses_fixture', 'session.execution.succeeded')
        state = self.dispatcher.reconcile('doc-1')
        self.assertEqual(state['state'], 'uncertain')
        self.assertIn('restarted', state['hazard'])
        self.assertEqual(len(self.api.posts), 1)

    def test_commit_verification_and_result_are_repeatable_and_detect_later_drift(self):
        self.submit()
        commit = self.commit()
        self.api.finish()
        state = self.dispatcher.verify('doc-1', commit)
        self.assertEqual(state['state'], 'verified')
        self.assertTrue(state['verification']['human_review_required'])
        self.assertEqual(self.dispatcher.verify('doc-1', commit)['state'], 'verified')
        self.assertIn('Task status: verified.', self.dispatcher.result('doc-1', patch=True)['patch'])
        self.assertEqual(self.dispatcher.result('doc-1')['verification']['base'], self.base)
        (self.repo / 'README.md').write_text('later drift\n')
        with self.assertRaisesRegex(DispatchError, 'drift'):
            self.dispatcher.result('doc-1')

    def test_out_of_scope_commit_is_not_verified(self):
        self.submit()
        commit = self.commit(extra=True)
        self.api.finish()
        with self.assertRaisesRegex(DispatchError, 'outside'):
            self.dispatcher.verify('doc-1', commit)

    def test_foreign_input_blocks_attribution(self):
        self.submit()
        self.api.append('ses_fixture', 'session.inbox.enqueued', inboxID='msg_foreign',
                        item={'type': 'user', 'delivery': 'steer', 'payload': {'text': 'unrelated'}})
        self.api.finish()
        state = self.dispatcher.reconcile('doc-1')
        self.assertEqual(state['state'], 'uncertain')
        self.assertIn('Another user', state['hazard'])

    def test_redirects_are_not_followed(self):
        self.api.redirect = True
        with self.assertRaisesRegex(DispatchError, '302'):
            Client(self.api.connection).json('GET', '/api/info')
        self.assertEqual(self.api.captured, 0)

    def test_unrelated_sqlite_database_is_not_modified(self):
        database = self.root / 'native.sqlite3'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('CREATE TABLE session_v2 (id TEXT)')
        database.chmod(0o600)
        before = database.read_bytes()
        with self.assertRaisesRegex(DispatchError, 'Not a dispatcher ledger'):
            Dispatcher(database)
        self.assertEqual(database.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
