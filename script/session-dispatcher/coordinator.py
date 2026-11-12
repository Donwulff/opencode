#!/usr/bin/env python3
"""Bounded notifications from the existing ledger to one existing Codex thread."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import stat
from string import Template
import subprocess
import sys
import threading
import time
import uuid

from batch import timestamp
from dispatch import DispatchError, digest, require
from operations import report, review_ready
from runner import process_identity, read_json, validate_directory, write_json
from review import RISKS


class CodexRPC:
    """Bun's installed WebSocket client bridges the existing Unix listener; no server spawn."""
    def __init__(self, config):
        path = Path(config['codex_socket']).resolve(strict=True)
        info = path.stat()
        parent = path.parent.stat()
        require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
                and (not info.st_mode & 0o077 or parent.st_uid == os.getuid() and not parent.st_mode & 0o077),
                'Codex socket must be owned and protected by socket or parent permissions')
        self.child = subprocess.Popen([config['bun'], str(Path(__file__).with_name('codex-rpc.js')), str(path)],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.child.stdout, selectors.EVENT_READ)
        self.buffer, self.sequence = bytearray(), 0
        try:
            self.call('initialize', {'clientInfo': {'name': 'opencode-coordinator', 'version': '1'},
                                     'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized', 'params': {}})
        except BaseException:
            self.close()
            raise

    def send(self, message):
        self.child.stdin.write((json.dumps(message) + '\n').encode())
        self.child.stdin.flush()

    def call(self, method, params):
        self.sequence += 1
        identifier = self.sequence
        self.send({'id': identifier, 'method': method, 'params': params})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, _, self.buffer = self.buffer.partition(b'\n')
                response = json.loads(line)
                if 'method' not in response and response.get('id') == identifier:
                    self.last_error = response.get('error')
                    require('result' in response, 'Codex rejected ' + method + '; outcome requires reconciliation')
                    return response['result']
                # Never answer approval/input requests or execute server-supplied commands.
            if not self.selector.select(max(0, deadline - time.monotonic())):
                break
            chunk = os.read(self.child.stdout.fileno(), 65536)
            require(chunk, 'Codex transport closed; outcome may be unknown')
            self.buffer.extend(chunk)
            require(len(self.buffer) <= 4 * 1024 * 1024, 'Codex frame exceeds read limit')
        raise DispatchError('Codex request timed out; outcome may be unknown')

    def close(self):
        self.child.stdin.close()
        try:
            self.child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.child.terminate()  # Only the owned transport helper; never Codex or agent execution.
            self.child.wait(timeout=3)
        self.child.stdout.close()
        self.selector.close()


def load_config(path):
    path = Path(path).resolve(strict=True)
    require(not path.stat().st_mode & 0o077, 'Coordinator config must be private')
    value = read_json(path)
    require(value.get('version') == 1, 'Unsupported coordinator configuration')
    validate_directory(value['database'], path.parent)
    require(not path.parent.stat().st_mode & 0o077, 'Coordinator directory must be private')
    value['root'] = str(path.parent)
    value['config_file'] = str(path)
    require(Path(value['repository']).resolve().is_dir(), 'Repository missing')
    require(timestamp(value['deadline']).tzinfo is not None, 'Explicit timezone required')
    for key, low, high in [('interval', 5, 60), ('min_ready', 1, 20), ('max_wait', 0, 3600),
                           ('cooldown', 0, 3600), ('max_wakes', 1, 20)]:
        require(type(value[key]) is int and low <= value[key] <= high, 'Invalid coordinator ' + key)
    require(value['thread_id'] and value['target_ref'], 'Explicit thread and target ref required')
    require(type(value.get('allow_resume', False)) is bool, 'allow_resume must be an explicit boolean')
    require(type(value.get('allow_effort_override', False)) is bool, 'allow_effort_override must be boolean')
    require(value.get('base_effort', 'medium') in ('medium', 'high', 'xhigh'), 'Invalid base effort')
    require(Path(value['codex_socket']).is_absolute(), 'Absolute Codex socket required')
    require(Path(value['bun']).is_absolute() and os.access(value['bun'], os.X_OK), 'Installed Bun required')
    return value


@contextmanager
def lock(database):
    fd = os.open(str(Path(database).resolve()) + '.coordinator.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as handle:
        require(not os.fstat(handle.fileno()).st_mode & 0o077, 'Coordinator lock must be private')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DispatchError('Another coordinator notifier owns this ledger') from None
        yield


class Coordinator:
    def __init__(self, config, client_factory=CodexRPC):
        self.config, self.client_factory = config, client_factory
        self.root = Path(config['root'])
        self.path = Path(str(Path(config['database']).resolve()) + '.coordinator.json')
        binding = {k: config[k] for k in ('database', 'repository', 'target_ref', 'thread_id', 'codex_socket')}
        binding['allow_resume'] = config.get('allow_resume', False)
        binding['root'] = str(self.root)
        binding['effort_policy'] = [config.get('allow_effort_override', False), config.get('base_effort', 'medium')]
        self.state = read_json(self.path) if self.path.exists() else {
            'kind': 'dispatcher-coordinator', 'binding': binding, 'entries': [], 'seen': [], 'first_pending': None}
        require(self.state.get('kind') == 'dispatcher-coordinator' and self.state['binding'] == binding,
                'Coordinator target/configuration drift; preserve the existing receipt and reconcile explicitly')

    def save(self, **fields):
        self.state.update(fields, heartbeat_at=datetime.now(timezone.utc).isoformat())
        write_json(self.path, self.state)

    def collect(self):
        c = self.config
        snapshot = report(c['database'], live=True, run_dir=c.get('runner_dir'))
        handoff = review_ready(c['database'], c['repository'], c['target_ref'], snapshot=snapshot)
        units = [r for r in handoff['units'] if r['verified_delivery']]
        attention = [{'task_id': r['task_id'], 'stage': r['stage'], 'task_state': r['task_state'],
                      'hold_reason': r.get('hold_reason'), 'cursor': r['event_cursor']}
                     for r in snapshot['tasks'] if r['worktree_reserved'] and (r['has_issue'] or r['has_hazard'])]
        for probe in snapshot['native_sessions']:
            if probe.get('form_count', 0):
                attention.append({'task_id': probe['task_id'], 'session_id': probe['session_id'],
                                  'pending_forms': probe['form_count']})
        keys = ['unit:' + r['task_id'] + ':' + str(r['final_commit']) for r in units]
        keys += ['attention:' + digest(r) for r in attention]
        escalation = None
        request = self.root / 'coordinator-escalation.json'
        if request.exists():
            value = read_json(request)
            require(value['thread_id'] == c['thread_id'] and value['risk'] in RISKS, 'Invalid escalation request')
            key = 'escalation:' + value['id']
            if key not in self.state['seen']:
                escalation = value
                keys.append(key)
        fresh = sorted(set(keys) - set(self.state['seen']))
        return {'target': handoff['target'], 'units': units, 'attention': attention,
                'queue': snapshot['counts']['queue'], 'runner': snapshot['runner'], 'keys': fresh, 'escalation': escalation,
                'meaning': 'Notification inputs only; source correctness and integration remain unapproved.'}

    def queue(self, client):
        entries, cursor = [], None
        for _ in range(20):
            result = client.call('thread/queue/list', {'threadId': self.config['thread_id'], 'limit': 100,
                                                      **({'cursor': cursor} if cursor else {})})
            entries.extend(result['data'])
            cursor = result.get('nextCursor')
            if not cursor:
                return entries
        raise DispatchError('Codex queue pagination incomplete; preserve pending user input')

    def consumed(self, client, message_id):
        cursor = None
        for _ in range(10):
            result = client.call('thread/items/list', {'threadId': self.config['thread_id'], 'limit': 20,
                                                      'sortDirection': 'desc',
                                                      **({'cursor': cursor} if cursor else {})})
            if any(r.get('type') == 'userMessage' and r.get('clientId') == message_id for r in result['data']):
                return True
            cursor = result.get('nextCursor')
            if not cursor:
                break
        return False  # Absence is not proof that generation/effects did not occur.

    def remember(self, entry, stage, **fields):
        entry.update(stage=stage, **fields)
        self.save(status='wake_' + stage)

    def observe_delivery(self, client, entry, status, pending):
        own = next((r for r in pending if r['clientUserMessageId'] == entry['id']), None)
        if self.consumed(client, entry['id']):
            self.remember(entry, 'observed_started', meaning='Native user input observed; no Git/effect success claim')
            return
        if entry['stage'] == 'admitting':
            if own:
                require(own['input'] == entry['input'], 'Native message ID has different input')
                self.remember(entry, 'admitted', submission=own['id'])
            else:
                self.remember(entry, 'uncertain', reason='Lost admission response; neither queue nor bounded history resolves it')
                return
        elif entry['stage'] in ('starting', 'uncertain'):
            self.remember(entry, 'uncertain', reason='Start/admission outcome unresolved; no automatic retry')
            return
        if not own:
            self.remember(entry, 'uncertain', reason='Queue entry absent; bounded history does not establish consumption')
            return
        if status != 'idle' or len(pending) != 1:
            self.save(status='waiting_for_native_queue_or_idle')
            return
        self.remember(entry, 'starting')  # Durable intent BEFORE any potentially billable RPC.
        result = client.call('thread/queue/start', {'threadId': self.config['thread_id'],
                                                  'queuedSubmissionId': own['id']})
        self.remember(entry, 'started', turn_id=result['turn']['id'],
                      meaning='Native turn admission only; task, Git and integration results remain separate')

    def tick(self, *, at=None, handoff=None):
        at = at or datetime.now(timezone.utc)
        c, s = self.config, self.state
        if at >= timestamp(c['deadline']):
            self.save(status='deadline_reached')
            return
        pending_entry = next((e for e in s['entries'] if e['stage'] in ('admitting','admitted','starting','uncertain')), None)
        handoff = handoff if handoff is not None else self.collect()
        self.save(last_check=at.isoformat(), pending_keys=handoff['keys'])
        if not pending_entry:
            if not handoff['keys']:
                self.save(status='no_new_review_work', first_pending=None)
                return
            if s['first_pending'] is None:
                self.save(first_pending=at.isoformat())
            if len(s['entries']) >= c['max_wakes']:
                self.save(status='wake_budget_reached')
                return
            last = s['entries'][-1]['at'] if s['entries'] else None
            if last and not handoff.get('escalation') and (at - timestamp(last)).total_seconds() < c['cooldown']:
                self.save(status='batching_cooldown')
                return
            drained = not (handoff['queue'].get('active', 0) or handoff['queue'].get('pending', 0))
            if (len(handoff['units']) < c['min_ready'] and not handoff['attention'] and not drained
                    and not handoff.get('escalation')
                    and (at - timestamp(s['first_pending'])).total_seconds() < c['max_wait']):
                self.save(status='batching_results')
                return
        client = self.client_factory(c)
        try:
            thread = client.call('thread/read', {'threadId': c['thread_id']})['thread']
            require(Path(thread['cwd']).resolve() == Path(c['repository']).resolve(), 'Codex thread repository drift')
            status = thread['status']['type']
            if status == 'notLoaded' and c.get('allow_resume', False):
                thread = client.call('thread/resume', {'threadId': c['thread_id'], 'excludeTurns': True})['thread']
                require(Path(thread['cwd']).resolve() == Path(c['repository']).resolve(), 'Resumed Codex repository drift')
                status = thread['status']['type']
            pending = self.queue(client)
            if pending_entry:
                self.observe_delivery(client, pending_entry, status, pending)
                return
            if status != 'idle' or pending:
                self.save(status='waiting_for_codex_idle_or_user_queue')
                return
            message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, digest([c['thread_id'], handoff['keys']])))
            if self.consumed(client, message_id):
                s['seen'] = sorted(set(s['seen']) | set(handoff['keys']))
                self.save(status='wake_already_observed', first_pending=None)
                return
            escalation = handoff.get('escalation')
            effort = RISKS[escalation['risk']] if escalation else c.get('base_effort', 'medium')
            if c.get('allow_effort_override', False):
                # Only a future-turn effort value on this explicitly owned coordinator.
                # This idempotent metadata RPC never starts a turn or changes model/permissions.
                client.call('thread/settings/update', {'threadId': c['thread_id'], 'effort': effort})
            path = self.root / ('handoff-' + message_id + '.json')
            write_json(path, handoff)
            template = Path(__file__).with_name('templates').joinpath('coordinator.txt').read_text()
            prompt = Template(template).substitute(handoff=str(path), database=c['database'], repository=c['repository'],
                                                  protected_worktrees=json.dumps(c.get('protected_worktrees', [])),
                                                  escalate_command=shlex.join([sys.executable, str(Path(__file__).resolve()), '--config', c.get('config_file', '<CONFIG>'), 'escalate']))
            entry = {'id': message_id, 'keys': handoff['keys'], 'at': at.isoformat(), 'stage': 'admitting',
                     'handoff': str(path), 'template_hash': digest(template),
                     'effort': effort if c.get('allow_effort_override', False) else 'inherited',
                     'escalation': bool(escalation),
                     'input': [{'type': 'text', 'text': prompt}]}
            s['entries'].append(entry)
            s['seen'] = sorted(set(s['seen']) | set(handoff['keys']))
            self.save(first_pending=None)
            result = client.call('thread/queue/add', {'threadId': c['thread_id'],
                                                     'clientUserMessageId': message_id, 'input': entry['input']})
            queued = result['queuedSubmission']
            require(queued['clientUserMessageId'] == message_id and queued['input'] == entry['input'],
                    'Native admission response disagrees with frozen wake input')
            self.remember(entry, 'admitted', submission=queued['id'])
            self.observe_delivery(client, entry, status, self.queue(client))
        finally:
            client.close()


def run(config):
    with lock(config['database']):
        coordinator = Coordinator(config)
        attempt = uuid.uuid4().hex
        coordinator.save(attempt=attempt, owner=process_identity(), status='starting',
                         code={n: hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest()
                               for n in ['coordinator.py', 'codex-rpc.js']})
        stopped = threading.Event()
        old = {sig: signal.signal(sig, lambda *_: stopped.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            while datetime.now(timezone.utc) < timestamp(config['deadline']):
                stop = Path(config['root']) / 'coordinator-stop.json'
                if stopped.is_set() or stop.exists() and read_json(stop).get('attempt') == attempt:
                    break
                try:
                    coordinator.tick()
                except (DispatchError, OSError, ValueError, KeyError, TypeError) as error:
                    coordinator.save(status='observation_or_delivery_error', error_type=type(error).__name__)
                stopped.wait(config['interval'])
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
            coordinator.save(status='exited')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['run', 'start', 'status', 'stop', 'account', 'escalate'])
    parser.add_argument('--wake-id')
    parser.add_argument('--reason')
    parser.add_argument('--risk', choices=sorted(RISKS))
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        root = Path(config['root'])
        if args.command == 'run':
            run(config)
        elif args.command == 'start':
            with lock(config['database']):
                state_path = Coordinator(config).path
                old = read_json(state_path).get('attempt') if state_path.exists() else None
                require(datetime.now(timezone.utc) < timestamp(config['deadline']), 'Coordinator window has ended')
            with (root / 'coordinator.log').open('ab') as log:
                os.chmod(root / 'coordinator.log', 0o600)
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--config',
                                          str(Path(args.config).resolve()), 'run'], stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                path = Coordinator(config).path
                if path.exists():
                    state = read_json(path)
                    if state.get('attempt') != old and state['owner']['pid'] == child.pid:
                        print(json.dumps({'owner': state['owner'], 'attempt': state['attempt'], 'deadline': config['deadline']}))
                        return 0
                require(child.poll() is None, 'Coordinator notifier exited during launch; inspect its log')
                time.sleep(0.05)
            raise DispatchError('Notifier launch unconfirmed; inspect status before retrying')
        elif args.command == 'escalate':
            require(config.get('allow_effort_override', False) and args.risk and args.reason, 'Enabled effort policy, risk and reason required')
            require(os.environ.get('CODEX_THREAD_ID') == config['thread_id'], 'Only the configured coordinator may request escalation')
            require(len(args.reason.encode()) <= 4096, 'Narrow the escalation evidence')
            coordinator = Coordinator(config)
            require(not any(e.get('escalation') for e in coordinator.state['entries']), 'One escalation per bounded coordinator window')
            path = root / 'coordinator-escalation.json'
            require(not path.exists(), 'An escalation request already exists; inspect its receipt')
            write_json(path, {'id': uuid.uuid4().hex, 'thread_id': config['thread_id'], 'risk': args.risk, 'reason': args.reason})
            print('Escalation requested. Finish this turn without deciding the affected integration; independent approved jobs continue.')
        elif args.command == 'account':
            require(args.wake_id and args.reason, 'Explicit wake ID and accounting reason required')
            with lock(config['database']):
                coordinator = Coordinator(config)
                entry = next((e for e in coordinator.state['entries'] if e['id'] == args.wake_id), None)
                require(entry and entry['stage'] in ('uncertain', 'admitting', 'admitted', 'starting'), 'Unresolved wake required')
                coordinator.remember(entry, 'manual_accounted', reason=args.reason)
                print('Wake accounted without resending or claiming tool/Git success; spent budget is retained.')
        elif args.command == 'status':
            print(json.dumps(Coordinator(config).state, indent=2))
        else:
            state = Coordinator(config).state
            write_json(root / 'coordinator-stop.json', {'attempt': state['attempt']})
            print('Notifier stop requested; Codex and OpenCode agent execution is untouched.')
        return 0
    except (DispatchError, OSError, ValueError, KeyError, TypeError) as error:
        print('coordinator: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
