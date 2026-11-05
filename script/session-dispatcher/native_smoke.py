#!/usr/bin/env python3
"""Opt-in current-source API smoke: disposable Git, native tools, loopback fixture provider.

Requires an already installed Bun and this checkout's existing dependencies.
Makes no requests to a real inference service or active OpenCode server.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from dispatch import Client, Dispatcher, encoded
from batch import Batch
from lifecycle import Lifecycle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--learning', action='store_true', help='Also exercise real forks, model selection and restricted learning')
    parser.add_argument('--batch', action='store_true', help='Exercise the approved queue plus native learning')
    args = parser.parse_args()
    if args.batch:
        args.learning = True
    bun = shutil.which('bun')
    if not bun:
        raise RuntimeError('Bun must already be installed; nothing will be installed')
    with tempfile.TemporaryDirectory(prefix='dispatch-native-') as directory:
        root = Path(directory)
        root.chmod(0o700)
        repo = root / 'repo'
        repo.mkdir()
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}

        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], env=env,
                                           stderr=subprocess.DEVNULL).decode().strip()

        git('init', '-b', 'fixture')
        with (repo / '.git/config').open('a') as file:
            file.write('\n[user]\n name = Dispatcher Fixture\n email = fixture@example.invalid\n[commit]\n gpgsign = false\n')
        (repo / 'README.md').write_text('Task status: pending.\n')
        (repo / 'AGENTS.md').write_text('Preserve the fixture.\n')
        git('add', 'README.md', 'AGENTS.md')
        git('commit', '-m', 'fixture base')
        calls = []
        actions = [
            ('read', {'path': str(repo / 'README.md')}),
            ('edit', {'path': str(repo / 'README.md'), 'oldString': 'pending.', 'newString': 'verified.'}),
            ('shell', {'command': "git add -- README.md && git commit -m 'docs: fixture result' -m 'OpenCode-Task: native-doc'",
                       'workdir': str(repo)}),
        ]

        if args.learning:
            actions += [
                None,  # Task completion is its own model turn, before the learning fork.
                ('shell', {'command': 'touch learning-must-not-exist', 'workdir': str(repo)}),
                ('read', {'path': str(repo / 'AGENTS.md')}),
                ('edit', {'path': str(repo / 'AGENTS.md'), 'oldString': 'Preserve the fixture.',
                          'newString': 'Preserve the fixture. Use exact receipt IDs.'}),
            ]

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                primary = bool(request.get('tools'))
                index = len(calls)
                if primary:
                    calls.append(index)
                delta = {'role': 'assistant', 'content': 'Fixture documentation assignment complete.'}
                finish = 'stop'
                if primary and index < len(actions) and actions[index] is not None:
                    name, arguments = actions[index]
                    delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': f'fixture_{index}', 'type': 'function',
                              'function': {'name': name, 'arguments': encoded(arguments)}}]}
                    finish = 'tool_calls'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for value in (
                    {'id': f'fixture_{index}', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                     'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                    {'id': f'fixture_{index}', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                     'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]},
                ):
                    self.wfile.write(('data: ' + encoded(value) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')

        provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        state = root / 'host'
        config = state / 'config/opencode'
        config.mkdir(parents=True)
        (config / 'opencode.json').write_text(encoded({
            'model': 'fixture/fixture',
            'providers': {'fixture': {'name': 'Disposable fixture', 'package': '@opencode/ai/providers/openai-compatible',
                'settings': {'baseURL': f'http://127.0.0.1:{provider.server_port}/v1'},
                'models': {name: {'name': name, 'limit': {'context': 32768, 'output': 2048}} for name in ('fixture', 'learner')}}},
            'agents': {'title': {'model': 'fixture/fixture'}, 'compaction': {'model': 'fixture/fixture'}},
            'restrict_to_configured_providers': True,
            'security': {'mode': 'strict', 'allow_local': True, 'allow_private_ip': False, 'models_dev_enabled': False},
            'update': 'disable', 'share': 'disabled', 'warming': False, 'username': 'fixture',
        }))
        host = None
        dispatcher = None
        try:
            with (root / 'host.log').open('w') as log:
                host = subprocess.Popen([bun, '--no-install', str(Path(__file__).with_name('serve.ts')), str(state)],
                                        cwd=root, env=env, stdout=log, stderr=log)
                deadline = time.monotonic() + 45
                while not (state / 'connection.json').exists():
                    if host.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Dedicated source host did not start; ' + (root / 'host.log').read_text()[-3500:])
                    time.sleep(0.05)
                client = Client(state / 'connection.json')
                session = client.json('POST', '/api/session', {'title': 'Dispatcher native fixture', 'agent': 'build',
                    'model': {'providerID': 'fixture', 'id': 'fixture'}, 'location': {'directory': str(repo)}})['data']
                dispatcher = Dispatcher(root / 'ledger.sqlite3')
                dispatcher.approve('fixture', session['id'], repo, state / 'connection.json')
                learn_prompt = root / 'learn.md'
                learn_prompt.write_text('Use only edit to preserve reusable lessons in AGENTS.md. No commands. Do not commit.\n')
                policy = {'prompt': str(learn_prompt), 'model': {'providerID': 'fixture', 'id': 'learner'}, 'allow': ['AGENTS.md']} if args.learning else None
                batch = None
                if args.batch:
                    batch = Batch(dispatcher)
                    batch.configure('fixture', state / 'connection.json', 'fixture', 1, 'available',
                                    (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
                    for _ in range(2):
                        batch.enqueue('native-doc', 'fixture', 'Change README task status from pending to verified.', ['README.md'], policy)
                    batch.tick()
                    first = dispatcher.status('native-doc')
                else:
                    first = dispatcher.submit('native-doc', 'fixture', 'Change README task status from pending to verified.', ['README.md'], learning=policy)
                repeated = dispatcher.submit('native-doc', 'fixture', 'Change README task status from pending to verified.', ['README.md'], learning=policy)
                assert first['message_id'] == repeated['message_id']
                deadline = time.monotonic() + 45
                while True:
                    status = dispatcher.reconcile('native-doc')
                    if status['state'] == 'completed' or status['hazard'] or time.monotonic() > deadline:
                        break
                    time.sleep(0.05)
                assert status['state'] == 'completed', encoded(status)
                verified = dispatcher.verify('native-doc', git('rev-parse', 'HEAD'))
                assert verified['state'] == 'verified', encoded(verified)
                result = dispatcher.result('native-doc', patch=True)
                assert 'Task status: verified.' in result['patch']
                admissions = dispatcher.db.execute("SELECT COUNT(*) FROM observed WHERE type='session.inbox.enqueued'").fetchone()[0]
                assert admissions == 1 and len(calls) == 4, (admissions, calls)
                learning_result = None
                if args.learning:
                    life = Lifecycle(dispatcher)
                    original_context = client.json('GET', f"/api/session/{session['id']}/context")['data']
                    original_model = client.json('GET', f"/api/session/{session['id']}")['data']['model']
                    deadline = time.monotonic() + 45
                    while True:
                        if batch:
                            batch.tick()
                            unit = life.unit('native-doc')
                        else:
                            unit = life.advance('native-doc', True)
                        if unit['stage'] in ('ready', 'needs_review') or time.monotonic() > deadline:
                            break
                        time.sleep(0.05)
                    assert unit['stage'] == 'ready', encoded(unit)
                    learning_result = life.result('native-doc')
                    fork = client.json('GET', f"/api/session/{unit['fork_id']}")['data']
                    fork_context = client.json('GET', f"/api/session/{unit['fork_id']}/context")['data']
                    assert fork['fork']['sessionID'] == session['id']
                    assert fork['location']['directory'] == str(repo) and fork['model']['id'] == 'learner'
                    assert len(fork_context) > len(original_context) > 0
                    assert client.json('GET', f"/api/session/{session['id']}")['data']['model'] == original_model
                    assert unit['detail']['learning_commit'] and not (repo / 'learning-must-not-exist').exists()
                    assert git('show', '-s', '--format=%P', unit['detail']['learning_commit']) == verified['verification']['commit']
                    failures = [event['data']['error'] for event in client.events(unit['fork_id'])
                                if event.get('type') == 'session.tool.failed']
                    assert any(item['category'] == 'tool_unavailable' for item in learning_result['feedback']), encoded({'fixture_failures': failures})
                    assert not json.loads(dispatcher.task(unit['child_id'])['payload']).get('command')
                    assert len(calls) == 8, calls
                if batch:
                    assert batch.tick()['phase'] == 'complete'
                print(json.dumps({'native_smoke': 'passed', 'batch': bool(batch), 'queued_admissions': admissions, 'provider_steps': len(calls),
                                  'state': verified['state'], 'retained_event_cursor': verified['cursor'],
                                  'git_scope_verified': True, 'fixture_only': True, 'learning': None if not args.learning else {
                                      'state': unit['stage'], 'outcome': unit['detail']['learning'], 'fork_history_preserved': True,
                                      'fork_only_model_override': True, 'shell_filtered_by_permissions': True, 'separate_commit': True}}, indent=2))
        finally:
            if dispatcher:
                dispatcher.close()
            if host and host.poll() is None:
                host.send_signal(signal.SIGINT)
                try:
                    host.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    host.kill()
                    host.wait(timeout=5)
            provider.shutdown()
            provider.server_close()
            thread.join()


if __name__ == '__main__':
    main()
