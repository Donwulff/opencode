#!/usr/bin/env python3
"""Read-only cross-server active-session preflight; never update Git or sessions."""
import argparse
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3

from dispatch import Client, DispatchError, now, require


def connections(database, state_dir, extra=()):
    """Discover ledger hosts and installed managed-service registrations, not just pools."""
    files = {str(Path(path).absolute()) for path in extra}
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        require(db.execute('PRAGMA application_id').fetchone()[0] == 0x4F434450,
                'Not a dispatcher ledger')
        db.execute('BEGIN')
        for snapshot, in db.execute('SELECT snapshot FROM registry'):
            files.add(str(Path(json.loads(snapshot)['connection']).absolute()))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='pool'").fetchone():
            files.update(str(Path(row[0]).absolute()) for row in db.execute('SELECT connection FROM pool'))
    managed = []
    state_dir = Path(state_dir)
    require(state_dir.is_dir(), 'Managed-service state directory is unavailable')
    for path in sorted(state_dir.glob('service*.json')):
        if path.name != 'service.json' and not path.name.startswith('service-'):
            continue
        # Probe even an old registration: its endpoint may still answer.
        files.add(str(path.absolute()))
        managed.append(str(path.absolute()))
    require(files, 'No OpenCode server connections discovered')
    return sorted(files), managed


def inspect(worktrees, files, *, managed=(), client_factory=Client):
    roots = sorted({str(Path(path).resolve(strict=True)) for path in worktrees})
    require(roots and files, 'Explicit worktrees and discovered server connections required')
    require(all(Path(root).is_dir() for root in roots), 'Worktree directories required')
    rows = {root: {'worktree': root, 'session_check': 'clear', 'active': []} for root in roots}
    servers = []
    for connection in sorted(set(map(str, files))):
        server = {'connection': connection, 'state': 'unknown'}
        try:
            client = client_factory(connection)
            active = client.json('GET', '/api/session/active')['data']
            require(isinstance(active, (dict, list)), 'Malformed active-session response')
            for sid in active:
                require(isinstance(sid, str) and re.fullmatch(r'ses_[A-Za-z0-9_-]+', sid),
                        'Malformed active session ID')
                info = client.json('GET', f'/api/session/{sid}')['data']
                directory = info['location']['directory']
                require(info['id'] == sid and isinstance(directory, str) and Path(directory).is_absolute(),
                        'Malformed active session placement')
                directory = Path(directory).resolve()
                for root in roots:
                    if directory.is_relative_to(root):
                        rows[root]['active'].append({'connection': connection, 'session_id': sid})
                        rows[root]['session_check'] = 'blocked'
            server.update(state='observed', active_count=len(active))
        except (DispatchError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
            # Never echo server text, credentials or raw errors into reports.
            server['error_type'] = type(error).__name__
            # Only an unreachable API AND definitely absent Linux owner make a
            # managed registration stale. Auth/schema failures are still unknown.
            if (connection in managed and isinstance(error, DispatchError)
                    and str(error) == 'API GET connection failed; outcome may be unknown'):
                try:
                    config = json.loads(Path(connection).read_text())
                    pid = config.get('pid') if isinstance(config, dict) else None
                    if type(pid) is int and pid > 0 and Path('/proc/self').exists() and not Path(f'/proc/{pid}').exists():
                        server['state'] = 'stale_owner_absent'
                except (OSError, ValueError):
                    pass
        servers.append(server)
    if any(server['state'] == 'unknown' for server in servers):
        for row in rows.values():
            if row['session_check'] == 'clear':
                row['session_check'] = 'unknown'
    return {'checked_at': now(), 'servers': servers, 'worktrees': list(rows.values()),
            'all_sessions_idle': all(row['session_check'] == 'clear' for row in rows.values()),
            'limits': 'Snapshot of discovered hosts only, not a reservation or Git/ledger clearance. '
                      'Supply --connection for unregistered standalone servers; recheck immediately before writing.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--worktree', action='append', required=True)
    parser.add_argument('--connection', action='append', default=[])
    parser.add_argument('--service-state', default=str(Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'opencode'))
    args = parser.parse_args()
    try:
        files, managed = connections(args.db, args.service_state, args.connection)
        result = inspect(args.worktree, files, managed=managed)
    except (DispatchError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        result = {'all_sessions_idle': False, 'session_check': 'unknown', 'error_type': type(error).__name__}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['all_sessions_idle'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
