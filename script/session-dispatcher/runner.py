#!/usr/bin/env python3
"""Own one bounded Batch.tick observer; never supervise or cancel agent execution."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

from dispatch import DispatchError, encoded, now, require


def lock_path(database):
    return Path(str(Path(database).resolve(strict=True)) + '.observer.lock')


def directory(database, run_dir=None):
    return Path(run_dir).absolute() if run_dir else Path(str(Path(database).absolute()) + '.runner')


def validate_directory(database, root):
    ledger = Path(database).absolute()
    require(ledger.is_file() and not ledger.is_symlink(), 'Existing ordinary ledger required')
    require(not ledger.stat().st_mode & 0o077, 'Ledger must be private (chmod 600)')
    require(not root.is_symlink(), 'Run directory must not be a symlink')
    with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)) as db:
        require(db.execute('PRAGMA application_id').fetchone()[0] == 0x4F434450, 'Not a dispatcher ledger')
        require(db.execute('PRAGMA user_version').fetchone()[0] in (3, 4, 5, 6),
                'Existing supported queue ledger required; runner never opts in a basic ledger')
        for snapshot, in db.execute('SELECT snapshot FROM registry'):
            worktree = Path(json.loads(snapshot)['worktree']).resolve()
            require(not root.resolve().is_relative_to(worktree), 'Keep runner state outside registered worktrees')


def read_json(path):
    require(not path.is_symlink() and path.stat().st_size <= 4 * 1024 * 1024, 'Invalid runner record')
    return json.loads(path.read_text())


def write_json(path, value):
    require(not path.is_symlink(), 'Runner records must not be symlinks')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as handle:
            handle.write(encoded(value) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def observer_lock(database):
    """Lifetime ownership, separate from Batch's short transaction/tick lock."""
    path = lock_path(database)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077, 'Observer lock must be private')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DispatchError('Another observer owns this ledger; do not start a second runner') from None
        yield handle


def process_identity():
    result = {'pid': os.getpid()}
    try:
        result.update(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                      pid_namespace=Path('/proc/self/ns/pid').stat().st_ino,
                      start_ticks=Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])
    except OSError:
        pass  # The lock/heartbeat remain authoritative; PID visibility is only diagnostic.
    return result


def code_identity():
    root = Path(__file__).parent
    files = sorted(p for p in root.glob('*.py') if not p.name.startswith('test_'))
    files += sorted((root / 'templates').glob('*.txt'))
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def inspect_run(database, *, run_dir=None, at=None):
    root, at = directory(database, run_dir), at or datetime.now(timezone.utc)
    state_path = root / 'state.json'
    if not state_path.exists():
        return {'state': 'unmanaged_or_no_record', 'run_dir': str(root)}
    try:
        value = read_json(state_path)
        require(value['database'] == str(Path(database).absolute()), 'Runner ledger mismatch')
        path = lock_path(database)
        ownership = 'unknown'
        if path.exists() and not path.is_symlink():
            with path.open('r') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    ownership = 'free'
                except BlockingIOError:
                    ownership = 'held'
                try:
                    owner = json.load(handle)
                except (ValueError, OSError):
                    owner = {}
            if ownership == 'held' and owner.get('attempt') != value['attempt']:
                ownership = 'other_run'
        age = (at - datetime.fromisoformat(value['heartbeat_at'])).total_seconds()
        freshness = 'fresh' if 0 <= age <= max(30, value['interval'] * 3) else 'stale_or_clock_changed'
        state = ('exited' if value['status'] == 'exited' else 'unobserved_exit' if ownership == 'free'
                 else 'running' if ownership == 'held' and freshness == 'fresh' else 'unknown')
        return {**value, 'state': state, 'ownership': ownership, 'freshness': freshness,
                'heartbeat_age_seconds': age, 'run_dir': str(root),
                'pid_visibility': 'not_used_to_infer_liveness_or_signal_authority'}
    except (DispatchError, OSError, ValueError, KeyError, TypeError) as error:
        return {'state': 'unknown', 'run_dir': str(root), 'error_type': type(error).__name__}


def request_stop(database, run_dir=None):
    root = directory(database, run_dir)
    value = read_json(root / 'state.json')
    require(value['database'] == str(Path(database).absolute()), 'Runner ledger mismatch')
    write_json(root / 'stop.json', {'attempt': value['attempt'], 'requested_at': now()})
    return {'requested': value['attempt'], 'effect': 'stop observation at a tick boundary; agents and pools unchanged'}


def run(database, *, run_dir=None, seconds=None, interval=None):
    from batch import Batch, timestamp
    from dispatch import Dispatcher
    root = directory(database, run_dir)
    validate_directory(database, root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not root.stat().st_mode & 0o077, 'Run directory must be private (chmod 700)')
    stopped = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stopped.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    dispatcher, state = None, None
    try:
        with observer_lock(database) as owner:
            config_path = root / 'config.json'
            if config_path.exists():
                config = read_json(config_path)
                require(config.get('kind') == 'dispatcher-observer' and config.get('version') == 1,
                        'Unrecognized run directory')
                require(config['database'] == str(Path(database).absolute()), 'Run directory belongs to another ledger')
                require(seconds is None and (interval is None or interval == config['interval']),
                        'Restart retains the original deadline/interval; omit --seconds or use a new run directory')
            else:
                require(type(seconds) is int and 0 <= seconds <= 86400, 'New run requires --seconds 0..86400')
                interval = 5 if interval is None else interval
                require(type(interval) is int and 1 <= interval <= 60, 'Interval must be 1..60 seconds')
                config = {'kind': 'dispatcher-observer', 'version': 1,
                          'database': str(Path(database).absolute()), 'interval': interval,
                          'deadline': (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()}
                write_json(config_path, config)
            dispatcher = Dispatcher(database)
            batch = Batch(dispatcher)
            attempt = uuid.uuid4().hex
            state = {**config, 'attempt': attempt, 'owner': process_identity(), 'code': code_identity(),
                     'runtime': {'python': sys.version, 'executable': sys.executable},
                     'schema_at_start': dispatcher.db.execute('PRAGMA user_version').fetchone()[0],
                     'started_at': now(), 'heartbeat_at': now(), 'last_successful_tick_at': None,
                     'status': 'starting', 'ticks': 0, 'phase': None}
            def save():
                write_json(root / 'state.json', state)
                write_json(root / ('attempt-' + attempt + '.json'), state)
            owner.seek(0)
            owner.truncate()
            owner.write(encoded({'attempt': attempt, 'run_dir': str(root)}) + '\n')
            owner.flush()
            os.fsync(owner.fileno())
            save()
            remaining = max(0, (timestamp(config['deadline']) - datetime.now(timezone.utc)).total_seconds())
            deadline = time.monotonic() + remaining
            code, reason = 2, 'observer_deadline'
            while time.monotonic() < deadline:
                stop_path = root / 'stop.json'
                if stopped.is_set() or stop_path.exists() and read_json(stop_path).get('attempt') == attempt:
                    code, reason = 0, 'observer_stop_requested'
                    break
                state.update(status='observing', heartbeat_at=now(), tick_started_at=now())
                save()
                try:
                    result = batch.tick()  # Existing recovery/reconciliation precedes pending admission.
                except Exception as error:
                    state['error_type'] = type(error).__name__
                    code, reason = 1, 'observer_error_effects_not_retried'
                    break
                state.update(heartbeat_at=now(), last_successful_tick_at=now(), phase=result['phase'],
                             ticks=state['ticks'] + 1, status='waiting')
                save()
                print(encoded(result), flush=True)
                if result['phase'] not in ('running', 'draining', 'waiting'):
                    code = 0 if result['phase'] == 'complete' else 1 if result['phase'] == 'blocked' else 2
                    reason = 'batch_' + result['phase']
                    break
                stopped.wait(min(config['interval'], max(0, deadline - time.monotonic())))
            state.update(status='exited', heartbeat_at=now(), exited_at=now(), exit_code=code, exit_reason=reason)
            save()
            return code
    finally:
        if dispatcher:
            dispatcher.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def start(database, *, run_dir=None, seconds=None, interval=None):
    root = directory(database, run_dir)
    validate_directory(database, root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not root.stat().st_mode & 0o077, 'Run directory must be private')
    state_path = root / 'state.json'
    old = read_json(state_path).get('attempt') if state_path.exists() else None
    args = [sys.executable, str(Path(__file__).resolve()), '--db', str(Path(database).absolute()),
            'run', '--run-dir', str(root)]
    for name, value in (('--seconds', seconds), ('--interval', interval)):
        if value is not None:
            args += [name, str(value)]
    fd = os.open(root / 'observer.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as log:
        child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if state_path.exists():
            value = read_json(state_path)
            if value['attempt'] != old and value['owner']['pid'] == child.pid:
                return {'started': value['attempt'], 'owner': value['owner'], 'run_dir': str(root),
                        'deadline': value['deadline'], 'status': value['status'],
                        'exit_code': value.get('exit_code'), 'agents_interrupted': False}
        if child.poll() is not None:
            raise DispatchError('Runner did not claim ownership; inspect its local observer.log; no automatic retry')
        time.sleep(0.05)
    raise DispatchError('Runner startup unconfirmed; inspect state/lock; do not launch another automatically')


def text_report(value):
    runner = value['runner']
    lines = [f"Observer: {runner['state']} (last successful tick: {runner.get('last_successful_tick_at') or 'unknown'})",
             'Queue: ' + ', '.join(f'{k}={v}' for k, v in sorted(value['counts']['queue'].items()))]
    for pool in value['pools']:
        lines.append(f"Pool {pool['name']}: {pool['admission_gate']}, capacity={pool['capacity']}, "
                     f"external={pool['external']}, occupied={pool.get('occupied', 'unknown')}, "
                     f"occupancy={pool['occupancy']}, until={pool['until_at']}")
        for sid, reasons in (pool['members'] or {}).items():
            lines.append('  ' + sid + ': ' + ', '.join(r['kind'] + ':' + (r['task_id'] or 'external') for r in reasons))
    for task in value['tasks']:
        if task['queue_state'] != 'ready' and task['worktree_reserved']:
            lines.append(f"{task['task_id']}: {task['queue_state']}, stage={task['stage']}, "
                         f"task={task['task_state']}, hold={task.get('hold_reason', 'none')}")
    for session in value['native_sessions']:
        if session.get('form_count') or session['state'] == 'unknown':
            lines.append(f"{session['session_id']}: pending_forms={session.get('form_count', 'unknown')}")
    historical = sum(t['queue_state'] == 'deferred' and not t['worktree_reserved'] for t in value['tasks'])
    verified = sum(bool(r['verified_delivery']) for r in value['ready_receipts'])
    lines.append(f"Deferred history without worktree reservations: {historical}")
    lines.append(f"Ready units: {verified} verified deliveries, {len(value['ready_receipts']) - verified} accounted checkpoints; "
                 "semantic review/integration remain separate.")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('run', 'start', 'stop', 'status'):
        command = commands.add_parser(name)
        command.add_argument('--run-dir')
        if name in ('run', 'start'):
            command.add_argument('--seconds', type=int)
            command.add_argument('--interval', type=int)
        if name == 'status':
            command.add_argument('--live', action='store_true')
            command.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'run':
            return run(args.db, run_dir=args.run_dir, seconds=args.seconds, interval=args.interval)
        if args.command == 'start':
            result = start(args.db, run_dir=args.run_dir, seconds=args.seconds, interval=args.interval)
        elif args.command == 'stop':
            result = request_stop(args.db, args.run_dir)
        else:
            from operations import report
            result = report(args.db, live=args.live, run_dir=args.run_dir)
            if not args.json:
                print(text_report(result))
                return 0
        print(json.dumps(result, indent=2))
        return 0
    except (DispatchError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print('runner: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
