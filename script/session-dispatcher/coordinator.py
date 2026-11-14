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
import re
import selectors
import shlex
import shutil
import signal
import stat
from string import Template
import subprocess
import sys
import tarfile
import threading
import time
import uuid

from batch import timestamp
from dispatch import DispatchError, digest, require
from operations import report, review_ready
from runner import process_identity, read_json, validate_directory, write_json
from review import RISKS


def same_input(actual, expected):
    # The native API serializes omitted text_elements as an empty array.
    # Preserve every other field and all text bytes in the equality check.
    return ([dict(item, text_elements=item.get('text_elements', [])) for item in actual]
            == [dict(item, text_elements=item.get('text_elements', [])) for item in expected])


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
                           ('cooldown', 0, 3600), ('max_wakes', 1, 512)]:
        require(type(value[key]) is int and low <= value[key] <= high, 'Invalid coordinator ' + key)
    require(value['thread_id'] and value['target_ref'], 'Explicit thread and target ref required')
    require(type(value.get('allow_resume', False)) is bool, 'allow_resume must be an explicit boolean')
    require(type(value.get('allow_effort_override', False)) is bool, 'allow_effort_override must be boolean')
    require(value.get('base_effort', 'medium') in ('medium', 'high', 'xhigh'), 'Invalid base effort')
    for key, default, high in [('backlog_target', 0, 100), ('urgent_cooldown', value['cooldown'], 3600),
                               ('max_escalations', 1, value['max_wakes'])]:
        setting = value.get(key, default)
        require(type(setting) is int and (1 if key == 'max_escalations' else 0) <= setting <= high,
                'Invalid coordinator ' + key)
    pools = value.get('refill_pools', [])
    require(isinstance(pools, list) and len(pools) <= 16
            and all(isinstance(p, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', p) for p in pools)
            and len(set(pools)) == len(pools),
            'Explicit refill pool names required')
    require(not value.get('backlog_target') or pools, 'Backlog target requires explicit refill pools')
    if value.get('shadow_policy'):
        from shadow import policy
        shadow = policy(value['shadow_policy'])
        require(all(Path(shadow[k]).resolve() == Path(value[k]).resolve() for k in ('database','repository'))
                and shadow['target_ref'] == value['target_ref'], 'Shadow coordinator binding mismatch')
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

    def replenishment(self, snapshot):
        """Read-only queue pressure, not technical task selection or admission."""
        result = []
        target = self.config.get('backlog_target', 0)
        for pool in snapshot['pools']:
            if (not target or pool['name'] not in self.config.get('refill_pools', [])
                    or pool['admission_gate'] != 'open' or pool['occupancy'] != 'observed'):
                continue
            tasks = [r for r in snapshot['tasks'] if r['task_pool'] == pool['name']
                     and r.get('purpose') not in ('shadow_review', 'planning')]
            independent = sorted(r['task_id'] for r in tasks
                                 if r['queue_state'] == 'pending' and not r['dependencies'])
            if len(independent) >= target:
                continue
            # No timestamps, cursors, learning stages or target HEAD: polling,
            # instruction views and our own documentation commits are not new work.
            progress = sorted((r['task_id'], r['queue_state']) for r in tasks)
            key = digest([pool['name'], pool['until_at'], target, progress])
            result.append({'pool': pool['name'], 'independent_pending': len(independent),
                           'target': target, 'available_slots': pool['available_slots'], 'key': key,
                           'meaning': 'Prepare useful authorized backlog; no dispatch or new scope approval.'})
        return result

    def collect(self):
        c = self.config
        snapshot = report(c['database'], live=True, run_dir=c.get('runner_dir'))
        handoff = review_ready(c['database'], c['repository'], c['target_ref'], snapshot=snapshot)
        units = [r for r in handoff['units'] if r['verified_delivery']]
        attention = [{'task_id': r['task_id'], 'stage': r['stage'], 'task_state': r['task_state'],
                      'hold_reason': r.get('hold_reason'), 'cursor': r['event_cursor']}
                     for r in snapshot['tasks'] if r.get('purpose') not in ('shadow_review', 'planning')
                     and r['worktree_reserved'] and (r['has_issue'] or r['has_hazard'])]
        for probe in snapshot['native_sessions']:
            if any(r['task_id'] == probe['task_id'] and r.get('purpose') == 'shadow_review' for r in snapshot['tasks']):
                continue
            if probe.get('form_count', 0):
                attention.append({'task_id': probe['task_id'], 'session_id': probe['session_id'],
                                  'pending_forms': probe['form_count']})
        keys = ['unit:' + r['task_id'] + ':' + str(r['final_commit']) for r in units]
        keys += ['attention:' + digest(r) for r in attention]
        replenishment = self.replenishment(snapshot)
        keys += ['backlog:' + r['key'] for r in replenishment]
        prerequisites = {edge['task'] for r in snapshot['tasks'] if r['queue_state'] == 'pending'
                         for edge in r['dependencies'] if edge['when'] == 'integrated'}
        dependency_ready = sorted(r['task_id'] for r in units if r['task_id'] in prerequisites)
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
        shadows = []
        if c.get('shadow_policy'):
            from shadow import policy
            root = Path(policy(c['shadow_policy'])['root'])
            for p in sorted(root.glob('case-*.json')):
                case = read_json(p)
                shadows.append({k:case.get(k) for k in ('id','source_task','input_hash','kind','stage')})
                shadows[-1]['reference_recorded'] = bool(case.get('reference'))
                # Do not expose the shadow judgment to the reference reviewer before its decision is frozen.
                if case.get('reference') and 'disagreement' in case:
                    shadows[-1].update(disagreement=case['disagreement'], result=case['result'])
                    if case['disagreement']:
                        key = 'shadow-disagreement:' + case['id'] + ':' + case['result_commit']
                        if key not in self.state['seen']:
                            fresh.append(key)
        return {'target': handoff['target'], 'units': units, 'attention': attention,
                'queue': snapshot['counts']['queue'], 'runner': snapshot['runner'], 'keys': fresh, 'escalation': escalation,
                'replenishment': replenishment, 'dependency_ready': dependency_ready,
                'shadow_policy': c.get('shadow_policy'), 'shadow_cases': shadows,
                'meaning': 'Notification inputs only; source correctness and integration remain unapproved.'}

    def request_escalation(self, risk, reason, issue=None):
        # Separate from the notifier's lifetime lock: parallel tool calls may
        # propose two issues, but must not replace an unconsumed request.
        with lock(self.root / 'escalation'):
            if self.path.exists():
                self.state = Coordinator(self.config).state
            return self._request_escalation(risk, reason, issue)

    def _request_escalation(self, risk, reason, issue):
        c, s = self.config, self.state
        require(c.get('allow_effort_override', False) and risk in RISKS and isinstance(reason, str) and reason.strip()
                and len(reason.encode()) <= 4096, 'Enabled effort policy, risk and bounded reason required')
        require(os.environ.get('CODEX_THREAD_ID') == c['thread_id'], 'Only the configured coordinator may request escalation')
        require(issue is None or isinstance(issue, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', issue),
                'Use a stable task or blocker ID for --issue')
        maximum = c.get('max_escalations', 1)
        require(maximum == 1 or issue, 'Multiple escalations require a stable --issue ID')
        require(len(s['entries']) < c['max_wakes'], 'Coordinator wake budget exhausted')
        require(sum(bool(e.get('escalation')) for e in s['entries']) < maximum,
                'Coordinator escalation budget exhausted')
        require(not issue or not any(e.get('escalation_issue') == issue for e in s['entries']),
                'Issue already escalated; preserve its result rather than retrying it')
        path = self.root / 'coordinator-escalation.json'
        if path.exists():
            previous = read_json(path)
            require('escalation:' + previous['id'] in s['seen'], 'An escalation request is still unresolved')
            require(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', previous['id']), 'Invalid previous escalation ID')
            archive = self.root / ('coordinator-escalation-' + previous['id'] + '.json')
            if not archive.exists():
                write_json(archive, previous)
        # Keep requests even after the pending pointer advances. An interrupted
        # pointer write is not permission to create another intent for this issue.
        for archive in self.root.glob('coordinator-escalation-*.json'):
            require(not issue or read_json(archive).get('issue') != issue, 'Issue already has an escalation intent')
        request = {'id': uuid.uuid4().hex, 'thread_id': c['thread_id'], 'risk': risk, 'reason': reason, 'issue': issue}
        write_json(self.root / ('coordinator-escalation-' + request['id'] + '.json'), request)
        write_json(path, request)
        return request

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
            for entry in result['data']:
                require(isinstance(entry.get('item'), dict) and entry.get('turnId'),
                        'Unsupported native history envelope; do not infer absent input')
                item = entry['item']
                if item.get('type') == 'userMessage' and item.get('clientId') == message_id:
                    return {'turn_id': entry['turnId'], 'user_message_id': item['id']}
            cursor = result.get('nextCursor')
            if not cursor:
                break
        return False  # Absence is not proof that generation/effects did not occur.

    def remember(self, entry, stage, **fields):
        entry.update(stage=stage, **fields)
        self.save(status='wake_' + stage)

    def observe_delivery(self, client, entry, status, pending):
        own = next((r for r in pending if r['clientUserMessageId'] == entry['id']), None)
        observed = self.consumed(client, entry['id'])
        if observed:
            self.remember(entry, 'observed_started', **observed, meaning='Native user input observed; no Git/effect success claim')
            return
        if entry['stage'] == 'admitting':
            if own:
                require(same_input(own['input'], entry['input']), 'Native message ID has different input')
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
            urgent = bool(handoff.get('replenishment') or handoff.get('dependency_ready'))
            cooldown = min(c['cooldown'], c.get('urgent_cooldown', c['cooldown'])) if urgent else c['cooldown']
            if last and not handoff.get('escalation') and (at - timestamp(last)).total_seconds() < cooldown:
                self.save(status='batching_cooldown')
                return
            drained = not (handoff['queue'].get('active', 0) or handoff['queue'].get('pending', 0))
            if (not urgent and len(handoff['units']) < c['min_ready'] and not handoff['attention'] and not drained
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
                     'escalation_issue': escalation.get('issue') if escalation else None,
                     'input': [{'type': 'text', 'text': prompt, 'text_elements': []}]}
            s['entries'].append(entry)
            s['seen'] = sorted(set(s['seen']) | set(handoff['keys']))
            self.save(first_pending=None)
            result = client.call('thread/queue/add', {'threadId': c['thread_id'],
                                                     'clientUserMessageId': message_id, 'input': entry['input']})
            queued = result['queuedSubmission']
            require(queued['clientUserMessageId'] == message_id and same_input(queued['input'], entry['input']),
                    'Native admission response disagrees with frozen wake input')
            self.remember(entry, 'admitted', submission=queued['id'])
            # An idle native queue can auto-start during queue/add itself.
            status = client.call('thread/read', {'threadId': c['thread_id']})['thread']['status']['type']
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
                    if config.get('shadow_policy'):
                        try:
                            from shadow import cycle
                            cycle(config['shadow_policy'])
                            coordinator.save(shadow_error=None)
                        except (DispatchError, OSError, ValueError, KeyError, TypeError,
                                subprocess.CalledProcessError, tarfile.TarError) as error:
                            coordinator.save(shadow_error=str(error)[:1024])
                    coordinator.tick()
                except (DispatchError, OSError, ValueError, KeyError, TypeError) as error:
                    coordinator.save(status='observation_or_delivery_error', error_type=type(error).__name__, error=str(error)[:1024])
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
    parser.add_argument('--issue', help='Stable task/blocker ID; at most one higher-effort turn per issue')
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
            coordinator = Coordinator(config)
            coordinator.request_escalation(args.risk, args.reason, args.issue)
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
