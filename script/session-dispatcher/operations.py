"""Read-only operational views of the existing ledger; never reconcile or admit work."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import sqlite3

from batch import capacity_members, timestamp
from dispatch import Client, DispatchError, require


def report(database, *, live=False, run_dir=None, at=None, client_factory=Client):
    at = at or datetime.now(timezone.utc)
    path = Path(database).absolute()
    require(not path.is_symlink() and path.is_file(), "Existing ordinary ledger required")
    require(path.stat().st_mode & 0o077 == 0, "Ledger must be private (chmod 600)")
    # Do not hold a live ledger read transaction across potentially slow native GETs.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as source:
        require(source.execute("PRAGMA application_id").fetchone()[0] == 0x4F434450, "Not a dispatcher ledger")
        db = sqlite3.connect(':memory:', isolation_level=None)
        try:
            source.backup(db)
        except BaseException:
            db.close()
            raise
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        require(db.execute("PRAGMA application_id").fetchone()[0] == 0x4F434450, "Not a dispatcher ledger")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        require(version in (3, 4, 5, 6), "Operational status requires a supported queue ledger")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tasks = {row['id']: dict(row) for row in db.execute("SELECT * FROM task")}
        units = {row['task_id']: dict(row) for row in db.execute("SELECT * FROM unit")}
        queued = [dict(row) for row in db.execute("SELECT * FROM queued ORDER BY created,task_id")]
        pools = [dict(row) for row in db.execute("SELECT * FROM pool ORDER BY name")]
        integrations = {row[0] for row in db.execute("SELECT task_id FROM integration WHERE stage NOT IN ('merged','deferred')")} \
            if 'integration' in tables else set()
        rows, sessions, ready = [], {}, []
        for row in queued:
            spec = json.loads(row['spec'])
            task, unit = tasks.get(row['task_id']), units.get(row['task_id'])
            detail = json.loads(unit['detail']) if unit else {}
            binding = spec['registration']
            reservations = []
            if row['state'] in ('pending', 'active', 'blocked', 'continuing'):
                reservations.append('queued_assignment')
            if task and task['state'] not in ('verified', 'accounted'):
                reservations.append('unresolved_task')
            if unit and unit['stage'] != 'ready':
                reservations.append('task_learning_unit')
            if row['task_id'] in integrations:
                reservations.append('integration')
            item = {'task_id': row['task_id'], 'queue_state': row['state'],
                    'task_state': task['state'] if task else 'not_submitted',
                    'stage': unit['stage'] if unit else 'pending',
                    'worktree': binding['worktree'], 'session_id': binding['session_id'],
                    'task_pool': spec['task_pool'], 'learning_pool': spec['learn_pool'],
                    'ledger_updated_at': task['updated'] if task else row['updated'],
                    'event_cursor': task['cursor'] if task else None,
                    'learning': detail.get('learning'),
                    'has_issue': bool(row['issue'] or detail.get('issue') or task and task['issue']),
                    'has_hazard': bool(task and task['hazard']),
                    'dependencies': spec.get('dependencies', []),
                    'worktree_reserved': bool(reservations), 'reservation_reasons': reservations}
            if row['state'] in ('blocked', 'deferred') or item['has_issue'] or item['has_hazard']:
                item['hold_reason'] = ('historical_accounted' if not reservations and task and task['state'] == 'accounted'
                                       else 'deferred_unsent' if not reservations and not task
                                       else 'execution_stopped_effects_unresolved' if task and task['terminal'] in ('failed', 'interrupted')
                                       else 'review_recorded_issue')
                item['detail_reference'] = {'command': 'dispatch.py status', 'task_id': row['task_id']}
            if unit and unit['stage'] == 'ready':
                proof = json.loads(task['verification']) if task and task['verification'] else {}
                ready.append({'task_id': row['task_id'], 'task_commit': proof.get('commit'),
                              'final_commit': detail.get('final_commit'), 'learning': detail.get('learning'),
                              'verified_delivery': bool(task and task['state'] == 'verified' and proof),
                              'verified_at': proof.get('verified_at'), 'unit_updated_at': unit['updated'],
                              'meaning': 'recorded_receipt_reference_not_fresh_worktree_verification'})
            elif row['state'] in ('pending', 'active', 'blocked', 'continuing'):
                sessions[(binding['connection'], binding['session_id'])] = row['task_id']
                if unit and unit['fork_id']:
                    sessions[(binding['connection'], unit['fork_id'])] = row['task_id']
            rows.append(item)
        # Read-only native probes are snapshots, not a heartbeat or admission decision.
        clients, pool_views, native = {}, [], []
        def client_for(connection):
            if connection not in clients:
                clients[connection] = client_factory(connection)
            return clients[connection]
        for pool in pools:
            view = {key: pool[key] for key in ('name', 'provider', 'capacity', 'external', 'state', 'until_at')}
            view['admission_gate'] = (pool['state'] if pool['state'] != 'available' else 'outside_window'
                                      if not pool['until_at'] or timestamp(pool['until_at']) <= at else 'open')
            view.update(occupancy='unknown', available_slots=None, members=None)
            if live:
                try:
                    client = client_for(pool['connection'])
                    members = capacity_members(db, pool, client)
                    view.update(occupancy='observed', checked_at=datetime.now(timezone.utc).isoformat(),
                                members=members, occupied=len(members) + pool['external'],
                                available_slots=max(0, pool['capacity'] - len(members) - pool['external']))
                except (DispatchError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
                    view.update(occupancy='unknown', error_type=type(error).__name__)
            pool_views.append(view)
        if live:
            for (connection, sid), owner in sorted(sessions.items()):
                probe = {'session_id': sid, 'task_id': owner, 'state': 'unknown'}
                try:
                    client = client_for(connection)
                    probe.update(state='observed', inbox_count=len(client.json('GET', f'/api/session/{sid}/inbox')['data']),
                                 form_count=len(client.json('GET', f'/api/session/{sid}/form')['data']),
                                 checked_at=datetime.now(timezone.utc).isoformat())
                except (DispatchError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
                    probe['error_type'] = type(error).__name__
                native.append(probe)
        from runner import inspect_run
        result = {'generated_at': at.isoformat(), 'schema': version, 'live_probes': live,
                  'counts': {'queue': dict(Counter(row['queue_state'] for row in rows)),
                             'stage': dict(Counter(row['stage'] for row in rows))},
                  'tasks': rows, 'pools': pool_views, 'native_sessions': native,
                  'ready_receipts': ready, 'runner': inspect_run(path, run_dir=run_dir, at=at),
                  'limits': ['Ledger update time is not a successful native observation timestamp.',
                             'Offline occupancy is unknown. Live snapshots do not authorize dispatch.',
                             'Issue bodies, prompts, tool output and connection credentials are omitted.']}
        if 'integration' in tables:
            result['integration_counts'] = dict(db.execute('SELECT stage,count(*) FROM integration GROUP BY stage'))
        return result
    finally:
        db.close()
