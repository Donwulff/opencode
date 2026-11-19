#!/usr/bin/env python3
"""Run one isolated, advisory Codex review per explicitly approved delivery."""

import argparse
from functools import partial
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import sys
import time

from coordinator import CodexRPC, load_config
from dispatch import Dispatcher, DispatchError, git, require
from review import DEFAULT_MODEL, MAX_INPUT_BYTES, SCHEMA, plan, run


def temporary_review(config, review, directory, timeout, *, client_factory=CodexRPC):
    client = client_factory(config)
    thread = None
    try:
        settings = client.call('thread/start', {
            'model': review['model'], 'allowProviderModelFallback': False,
            'cwd': str(directory), 'approvalPolicy': 'never', 'sandbox': 'read-only',
            'ephemeral': True, 'environments': [],
            'config': {
                'features.multi_agent': False, 'features.multi_agent_v2': False,
                'features.shell_tool': False, 'features.unified_exec': False,
                'features.apps': False, 'features.plugins': False, 'features.hooks': False,
                'features.unbounded_connection_retries': False, 'web_search': 'disabled',
            },
        })
        thread = settings['thread']['id']
        require(settings['thread']['ephemeral'] and not settings['thread'].get('path')
                and thread != config['thread_id'] and settings['model'] == review['model']
                and settings['approvalPolicy'] == 'never' and settings['sandbox']['type'] == 'readOnly',
                'Codex did not confirm an isolated read-only review context')
        events = []

        def capture(message):
            params = message.get('params', {})
            if (message.get('method') in ('item/completed', 'turn/completed')
                    and params.get('threadId') == thread and 'id' not in message):
                require(len(events) < 64, 'Review emitted too many events')
                events.append(message)

        deadline = time.monotonic() + timeout
        response = client.call('turn/start', {
            'threadId': thread, 'model': review['model'], 'effort': review['effort'],
            'outputSchema': SCHEMA, 'input': [{'type': 'text', 'text': review['prompt']}],
        }, on_notification=capture)
        turn = response['turn']['id']
        answer = None
        while time.monotonic() < deadline:
            if not events:
                capture(client.receive(deadline))
                continue
            message = events.pop(0)
            params = message['params']
            if message['method'] == 'turn/completed' and params['turn']['id'] == turn:
                require(params['turn']['status'] == 'completed' and answer is not None,
                        'Temporary review did not return a confirmed final answer')
                return answer
            if message['method'] == 'item/completed' and params['turnId'] == turn:
                item = params['item']
                require(item['type'] in ('userMessage', 'agentMessage', 'reasoning'),
                        'Temporary review attempted an out-of-scope action')
                if item['type'] == 'agentMessage' and item.get('phase') in (None, 'final_answer'):
                    answer = item['text']
        raise DispatchError('Temporary review reached its deadline; outcome may be unknown')
    finally:
        try:
            if thread is not None and thread != config['thread_id']:
                client.call('thread/unsubscribe', {'threadId': thread})
        except (DispatchError, OSError):
            pass  # Outcome comes from the completed turn and durable review receipt.
        finally:
            client.close()


def review_context(repository, assignment, pins):
    require(isinstance(pins, list), 'Review context must list pinned Git files')
    files = []
    size = 0
    for pin in pins:
        require(isinstance(pin, dict) and set(pin) == {'commit', 'path', 'sha256'},
                'Each context file requires commit, path and sha256')
        commit, path, checksum = pin['commit'], pin['path'], pin['sha256']
        require(isinstance(commit, str) and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit)
                and isinstance(checksum, str) and re.fullmatch(r'[0-9a-f]{64}', checksum),
                'Context files require full commit and SHA-256 pins')
        require(isinstance(path, str) and path and not PurePosixPath(path).is_absolute()
                and '..' not in PurePosixPath(path).parts and '\0' not in path,
                'Invalid Git context path')
        require(git(repository, 'rev-parse', '--verify', commit + '^{commit}').decode().strip() == commit,
                'Context pin must identify the commit itself')
        obj = f'{commit}:{path}'
        require(git(repository, 'cat-file', '-t', obj).strip() == b'blob', 'Review context must name a file')
        size += int(git(repository, 'cat-file', '-s', obj))
        require(size <= MAX_INPUT_BYTES, 'Pinned review context exceeds the review input bound')
        content = git(repository, 'cat-file', 'blob', obj)
        require(sha256(content).hexdigest() == checksum, f'Review context hash mismatch for {path}')
        files.append({**pin, 'text': content.decode()})
    return {'original_assignment': assignment['text'],
            'declared_dependencies': assignment.get('dependencies', []), 'files': files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Existing private coordinator configuration')
    parser.add_argument('--evidence', required=True, help='Operator-reviewed scope and evidence summary')
    parser.add_argument('--context-file', help='JSON map from task ID to reviewed Git file pins')
    parser.add_argument('--seconds', type=int, default=3600)
    parser.add_argument('task_ids', nargs='+', help='Exactly the delivery IDs authorized for review')
    args = parser.parse_args()
    config = load_config(args.config)
    deadline = time.monotonic() + args.seconds
    evidence = Path(args.evidence).read_text()
    pins = json.loads(Path(args.context_file).read_text()) if args.context_file else {}
    pending = dict.fromkeys(args.task_ids)
    backend = partial(temporary_review, config)
    failed = False
    dispatcher = Dispatcher(config['database'])
    try:
        require(1 <= args.seconds <= 86400 and evidence.strip(), 'Invalid watch bound or empty evidence')
        require(isinstance(pins, dict) and pins.keys() <= pending.keys(),
                'Review context may include only explicitly selected task IDs')
        assignments = {}
        for task_id in pending:
            require(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', task_id), 'Invalid task ID')
            row = dispatcher.db.execute('SELECT spec FROM queued WHERE task_id=?', (task_id,)).fetchone()
            assignment = json.loads(row['spec']) if row else None
            require(assignment and assignment.get('purpose', 'delivery') == 'delivery',
                    'An explicitly queued delivery is required')
            assignments[task_id] = assignment
        while pending and time.monotonic() < deadline:
            for task_id in list(pending):
                existing = dispatcher.db.execute("SELECT 1 FROM sqlite_master WHERE name='review'").fetchone()
                if existing:
                    prior = dispatcher.db.execute('SELECT state FROM review WHERE task_id=? ORDER BY attempt DESC LIMIT 1',
                                                  (task_id,)).fetchone()
                    if prior:
                        print(json.dumps({'task': task_id, 'previous_review': prior['state']}), flush=True)
                        failed |= prior['state'] not in ('reviewed', 'reviewed_with_risks')
                        del pending[task_id]
                        continue
                row = dispatcher.db.execute('''SELECT q.state AS queue, u.stage FROM queued q
                                               LEFT JOIN unit u USING(task_id) WHERE q.task_id=?''', (task_id,)).fetchone()
                if row['stage'] != 'ready' and row['stage'] != 'needs_review' and row['queue'] not in ('blocked', 'deferred'):
                    continue
                del pending[task_id]
                try:
                    require(row['queue'] == 'ready' and row['stage'] == 'ready', 'Delivery did not reach reviewable state')
                    remaining = int(deadline - time.monotonic())
                    require(remaining > 0, 'Review window expired before admission')
                    context = review_context(config['repository'], assignments[task_id], pins.get(task_id, []))
                    frozen = plan(dispatcher, task_id, DEFAULT_MODEL, [], evidence, context=context)
                    result = run(dispatcher, task_id, frozen, timeout=min(600, remaining), app_server=backend)
                    print(json.dumps({'task': task_id, 'state': result['state']}), flush=True)
                    failed |= result['state'] not in ('reviewed', 'reviewed_with_risks')
                except (DispatchError, OSError, ValueError, KeyError, TypeError) as error:
                    print(json.dumps({'task': task_id, 'issue': str(error)}), flush=True)
                    failed = True
            if pending:
                time.sleep(max(0, min(10, deadline - time.monotonic())))
        if pending:
            print(json.dumps({'window_expired': list(pending)}), flush=True)
        return 1 if failed else 2 if pending else 0
    finally:
        dispatcher.close()


if __name__ == '__main__':
    sys.exit(main())
