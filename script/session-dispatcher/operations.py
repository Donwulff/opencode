"""Read-only operational views of the existing ledger; never reconcile or admit work."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import http.client
import json
import math
import re
from pathlib import Path
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from batch import capacity_members, timestamp
from dispatch import Client, DispatchError, NoRedirect, require


def inference_status(url, *, timeout=3):
    """Bounded local metadata GETs only; never submit inference or change admission."""
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path in ('', '/'), 'Inference status requires an explicit loopback HTTP root URL')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'checks': {},
              'models': [], 'metrics': {}, 'cache': {},
              'limits': 'Metadata only: no generation test or total GPU-memory headroom measurement.'}
    names = {'num_requests_running', 'num_requests_waiting', 'kv_cache_usage_perc',
             'num_preemptions_total', 'prompt_tokens_total', 'generation_tokens_total'}
    for path, limit in [('/health', 1024), ('/v1/models', 65536), ('/metrics', 1024 * 1024)]:
        try:
            request = urllib.request.Request(url.rstrip('/') + path, method='GET')
            with opener.open(request, timeout=timeout) as response:
                raw, status = response.read(limit + 1), response.status
            require(len(raw) <= limit, 'Inference metadata response exceeds read limit')
            if path == '/v1/models':
                data = json.loads(raw)['data']
                require(isinstance(data, list) and 0 < len(data) <= 50, 'Invalid model metadata')
                require(all(isinstance(item, dict) and isinstance(item.get('id'), str)
                            and len(item['id']) <= 256 for item in data), 'Invalid model ID')
                result['models'] = [item['id'] for item in data]
            elif path == '/metrics':
                for line in raw.decode().splitlines():
                    match = re.fullmatch(r'vllm:([a-z_]+)(?:\{.*\})?\s+(\S+)(?:\s+\d+)?', line)
                    if match and match[1] in names:
                        value = float(match[2])
                        if math.isfinite(value):
                            result['metrics'].setdefault(match[1], []).append(value)
                    if line.startswith('vllm:cache_config_info{'):
                        for name in ['kv_cache_memory_bytes', 'kv_cache_size_tokens', 'kv_cache_max_concurrency']:
                            found = re.search(r'(?:[,{])' + name + r'="([0-9.eE+-]+)"', line)
                            if found and math.isfinite(float(found[1])):
                                result['cache'].setdefault(name, []).append(float(found[1]))
            result['checks'][path] = {'state': 'ok', 'http_status': status}
        except (DispatchError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
            result['checks'][path] = {'state': 'unavailable', 'error_type': type(error).__name__}
            if isinstance(error, urllib.error.HTTPError):
                result['checks'][path]['http_status'] = error.code
                error.close()
    result['state'] = ('responding' if all(result['checks'][path]['state'] == 'ok'
                                         for path in ('/health', '/v1/models')) else 'unavailable')
    return result


def report(database, *, live=False, run_dir=None, at=None, client_factory=Client, inference_url=None):
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
            # Outcomes belong to the current observed run, not historical task attempts.
            item['run_tool_outcomes'] = None
            if task and task['run_start'] is not None:
                item['run_tool_outcomes'] = dict(db.execute(
                    "SELECT type,count(*) FROM observed WHERE task_id=? AND seq>? "
                    "AND type IN ('session.tool.success','session.tool.failed') GROUP BY type",
                    (task['id'], task['run_start'])))
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
        if inference_url is not None:
            result['inference'] = inference_status(inference_url)
        if 'integration' in tables:
            result['integration_counts'] = dict(db.execute('SELECT stage,count(*) FROM integration GROUP BY stage'))
        return result
    finally:
        db.close()
