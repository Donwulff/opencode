"""Opt-in proposal intake and backlog replenishment inside the existing observer.

Agent proposals are data under an operator-owned scope policy, never authority to
merge, approve dependencies, install packages or retry effects. Uses the existing
queue, API, task proofs and learning lifecycle; no inference client or server.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

from batch import Batch, timestamp
from dispatch import DispatchError, digest, encoded, git, identity, now, require
from operations import report
from runner import read_json
from worktree_guard import connections, inspect


def policy(path):
    path = Path(path).resolve(strict=True)
    require(not path.stat().st_mode & 0o077, 'Autonomy policy must be private')
    c = read_json(path)
    require(c.get('version') == 1 and re.fullmatch(r'[a-z0-9-]{1,48}', c['id']), 'Invalid autonomy policy')
    for key, low, high in [('backlog', 1, 32), ('max_tasks', 1, 512), ('max_worktrees', 0, 64)]:
        require(type(c[key]) is int and low <= c[key] <= high, 'Invalid ' + key)
    require(c['scopes'] and isinstance(c['scopes'], dict), 'Explicit authorized scopes required')
    require(timestamp(c['until']).tzinfo is not None, 'Explicit autonomy deadline required')
    root = Path(c['root']).resolve(strict=True)
    require(not root.stat().st_mode & 0o077 and not root.is_relative_to(Path(c['repository']).resolve()),
            'Keep autonomy state private and outside the project')
    require(git(c['repository'], 'rev-parse', c['base'] + '^{commit}').decode().strip() == c['base'], 'Pin task inputs')
    return c


def proposal(value, config):
    require(isinstance(value, dict) and set(value) == {'key', 'scope', 'kind', 'objective', 'references', 'dependencies'},
            'Proposal schema mismatch')
    require(isinstance(value['key'], str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{2,95}', value['key']), 'Invalid work key')
    require(value['scope'] in config['scopes'] and value['kind'] in ('offline', 'fixture', *config.get('native_profiles', {})), 'Unapproved scope/recipe')
    require(value['kind'] not in config.get('native_profiles', {}) or value['scope'] in config.get('native_scopes', []),
            'Native scope requires explicit profile authorization')
    require(isinstance(value['objective'], str) and 40 <= len(value['objective']) <= 6000, 'Bounded concrete objective required')
    require(isinstance(value['references'], list) and 1 <= len(value['references']) <= 20, 'Pinned references required')
    for path in value['references']:
        require(isinstance(path, str) and len(path) <= 300 and not PurePosixPath(path).is_absolute()
                and '..' not in PurePosixPath(path).parts, 'Unsafe reference')
    require(isinstance(value['dependencies'], list) and len(value['dependencies']) <= 8, 'Invalid dependencies')
    for dep in value['dependencies']:
        require(isinstance(dep, dict) and set(dep) == {'task', 'when'}
                and isinstance(dep['task'], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', dep['task'])
                and dep['when'] in ('verified', 'integrated', 'approved'), 'Invalid prerequisite')
    return value


class Autonomy:
    def __init__(self, dispatcher, config, *, activity=None):
        self.d, self.c = dispatcher, config
        self.batch = Batch(dispatcher)
        self.activity = activity or self.cross_server_idle
        with self.d.atomic():
            self.d.db.execute('''CREATE TABLE IF NOT EXISTS autonomy_item (
                policy TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
                task_id TEXT UNIQUE, source TEXT, detail TEXT NOT NULL, updated TEXT NOT NULL,
                PRIMARY KEY(policy,key))''')
            self.d.db.execute('''CREATE TABLE IF NOT EXISTS autonomy_seen (
                policy TEXT NOT NULL, task_id TEXT NOT NULL, commit_id TEXT NOT NULL, detail TEXT NOT NULL,
                PRIMARY KEY(policy,task_id))''')

    def cross_server_idle(self, root):
        files, managed = connections(self.d.path, self.c['service_state'])
        return inspect([root], files, managed=managed)['all_sessions_idle']

    def save(self, key, state, detail, task=None):
        old = self.d.db.execute('SELECT detail FROM autonomy_item WHERE policy=? AND key=?', (self.c['id'],key)).fetchone()
        detail = {**(json.loads(old[0]) if old else {}), **detail}
        self.d.db.execute('UPDATE autonomy_item SET state=?,detail=?,task_id=coalesce(?,task_id),updated=? WHERE policy=? AND key=?',
                          (state, encoded(detail), task, now(), self.c['id'], key))

    def add(self, value, source):
        value = proposal(value, self.c)
        for path in value['references']:
            git(self.c['repository'], 'cat-file', '-e', self.c['base'] + ':' + path)
        for dep in value['dependencies']:
            require(self.d.db.execute('SELECT 1 FROM queued WHERE task_id=?', (dep['task'],)).fetchone(),
                    'Prerequisite must already exist; no guessed task IDs')
        old = self.d.db.execute('SELECT payload FROM autonomy_item WHERE policy=? AND key=?',
                                (self.c['id'], value['key'])).fetchone()
        if old:
            require(old[0] == encoded(value), 'Work key already names a different proposal')
            return False
        n = self.d.db.execute('SELECT count(*) FROM autonomy_item WHERE policy=?', (self.c['id'],)).fetchone()[0]
        require(n < self.c['max_tasks'], 'Autonomy task budget reached')
        self.d.db.execute('INSERT INTO autonomy_item VALUES (?,?,?,\'pending\',NULL,?,\'{}\',?)',
                          (self.c['id'], value['key'], encoded(value), source, now()))
        return True

    def consume(self, snapshot):
        for receipt in snapshot['ready_receipts']:
            task = receipt['task_id']
            if not receipt['verified_delivery'] or receipt.get('purpose') == 'shadow_review':
                continue
            row = self.d.db.execute('SELECT * FROM autonomy_item WHERE policy=? AND task_id=?',
                                    (self.c['id'], task)).fetchone()
            if not row or self.d.db.execute('SELECT 1 FROM autonomy_seen WHERE policy=? AND task_id=?', (self.c['id'], task)).fetchone():
                continue
            detail = {'commit': receipt['final_commit'], 'accepted': [], 'rejected': []}
            path = 'integration/' + task + '/followups.json'
            capture = subprocess.run(['git', '-C', self.c['repository'], 'show', receipt['final_commit'] + ':' + path],
                                     capture_output=True)
            if capture.returncode == 0:
                try:
                    require(len(capture.stdout) <= 65536, 'Oversized proposals')
                    body = json.loads(capture.stdout)
                    require(isinstance(body, dict) and set(body) == {'tasks', 'reason'}
                            and isinstance(body['tasks'], list) and len(body['tasks']) <= 8
                            and isinstance(body['reason'], str), 'Invalid follow-up envelope')
                    for value in body['tasks']:
                        try:
                            if self.add(value, task): detail['accepted'].append(value['key'])
                        except (DispatchError, KeyError, TypeError, ValueError) as error:
                            detail['rejected'].append(str(error)[:300])
                except (DispatchError, KeyError, TypeError, ValueError) as error:
                    detail['rejected'].append(str(error)[:300])
            else:
                detail['no_followup_file'] = True
            self.d.db.execute('INSERT INTO autonomy_seen VALUES (?,?,?,?)',
                              (self.c['id'], task, receipt['final_commit'], encoded(detail)))
            self.save(row['key'], 'completed', detail)

    def worker(self, snapshot):
        busy = {t['worktree'] for t in snapshot['tasks'] if t['worktree_reserved']}
        receipts = snapshot['ready_receipts']
        for name in self.c['workers']:
            row = self.d.db.execute('SELECT snapshot FROM registry WHERE name=?', (name,)).fetchone()
            if not row: continue
            reg = json.loads(row[0]); root = reg['worktree']
            if root in busy or root in self.c['protected_worktrees']: continue
            local = identity(root)
            if local['status'] or not self.activity(root): continue
            try:
                self.d.check_lease(local)
                client = self.d.client_factory(reg['connection'])
                self.d.check_binding(reg, client)
                require(client.idle(reg['session_id']), 'Native session busy')
                require(local['head'] == self.c['base'], 'Automatic reuse requires the explicit pinned base')
                # Never advance a delivered unit's branch before content review.
                require(all(subprocess.run(['git', '-C', root, 'merge-base', '--is-ancestor', r['final_commit'], self.c['base']],
                                           capture_output=True).returncode == 0
                            for r in receipts if any(t['task_id']==r['task_id'] and t['worktree']==root for t in snapshot['tasks'])), 'Unintegrated deliverable retains its branch')
                require(reg['model'] == self.c['model'], 'Model drift')
                return name, reg
            except DispatchError:
                continue
        return None

    def new_worker(self, key, task):
        used = self.d.db.execute("SELECT count(*) FROM autonomy_item WHERE policy=? AND json_extract(detail,'$.new_worktree') IS NOT NULL",
                                 (self.c['id'],)).fetchone()[0]
        require(used < self.c['max_worktrees'], 'Unreviewed-worktree capacity reached; integration required')
        root = Path(self.c['root']) / task
        require(not root.exists(), 'Unexpected existing worker; inspect before recovery')
        sid = 'ses_auto' + digest([self.c['id'], key])[:24]
        detail = {'new_worktree': str(root), 'session_id': sid}
        self.save(key, 'preparing', detail, task)  # Durable intent before filesystem/API effects.
        subprocess.run(['git', '-C', self.c['repository'], 'worktree', 'add', '-b', task, str(root), self.c['base']],
                       check=True, capture_output=True)
        self.save(key, 'creating_session', detail)
        client = self.d.client_factory(self.c['connection'])
        session = client.json('POST', '/api/session', {'id': sid, 'title': 'Qwen: ' + key, 'agent': 'build',
                             'location': {'directory': str(root)}, 'model': self.c['model']})['data']
        require(session['id'] == sid, 'Session-create result mismatch; no retry')
        require(self.activity(str(root)), 'Cross-server worker activity must be clear before approval')
        self.d.approve(task, sid, str(root), self.c['connection'])
        reg = json.loads(self.d.db.execute('SELECT snapshot FROM registry WHERE name=?', (task,)).fetchone()[0])
        return task, reg, detail

    def prompt(self, item, task, source):
        template = Path(__file__).with_name('templates').joinpath('autonomy.txt').read_text()
        context = {'base': self.c['base'], 'scope': item['scope'], 'scope_authorization': self.c['scopes'][item['scope']],
                   'recipe': item['kind'], 'objective': item['objective'], 'references': item['references'],
                   'allowed_scopes': self.c['scopes'], 'task': task}
        context['known_work'] = [dict(r) for r in self.d.db.execute('SELECT key,state,task_id FROM autonomy_item WHERE policy=?', (self.c['id'],))]
        if source not in ('planner','operator-policy'):
            context['parent_delivery'] = next((r for r in report(self.d.path)['ready_receipts'] if r['task_id']==source), None)
        boundary = self.c.get('native_profiles', {}).get(item['kind'], 'OFFLINE ONLY: no container/socket/network/native execution or package/source/environment changes. fixture permits host-only tests after confirming all external/native effects are mocked.')
        context['current_assignments'] = [{k:t.get(k) for k in ('task_id','queue_state','worktree','dependencies')} for t in report(self.d.path)['tasks'] if t['queue_state'] in ('active','pending','blocked','continuing')]
        return template.replace('$EXECUTION_BOUNDARY',boundary).replace('TASK_ID',task) + '\nOperator-policy-bound task data:\n' + json.dumps(context, indent=2)

    def tick(self):
        fd = os.open(str(Path(self.c['root']) / 'writer.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'r+') as handle:
            try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise DispatchError('Another autonomy writer owns this policy') from None
            return self._tick()

    def _tick(self):
        at = datetime.now(timezone.utc)
        snapshot = report(self.d.path)
        self.consume(snapshot)
        for value in self.c.get('seeds', []):
            try: self.add(value, 'operator-policy')
            except DispatchError as error:
                if 'already names a different' in str(error): raise
        if self.c.get('shadow_policy'):
            from shadow import Shadow, policy as shadow_policy
            shadow = Shadow(self.d, shadow_policy(self.c['shadow_policy']))
            shadow.collect()
            count = self.d.db.execute("SELECT count(*) FROM queued WHERE state IN ('pending','active') AND json_extract(spec,'$.purpose')='shadow_review'").fetchone()[0]
            if count < self.c.get('shadow_backlog', 2) and at < timestamp(self.c['until']):
                cases = {c['source_task'] for c in shadow.cases()}
                candidates = self.c.get('shadow_seeds', []) + [r['task_id'] for r in snapshot['ready_receipts']
                    if r.get('purpose', 'delivery') == 'delivery' and r['task_id'].startswith('auto-')]
                for task in candidates:
                    if task not in cases:
                        try:
                            historical = self.c.get('shadow_references', {}).get(task)
                            case = shadow.capture(task, target=historical.get('target') if historical else self.c['base'],
                                                  kind='cold_replay' if task in self.c.get('shadow_seeds', []) else 'forward')
                            if historical and not case.get('reference'):
                                shadow.reference(case['id'],case['input_hash'],historical['assessment'],
                                                 'Retrospective reference from preserved prior review, not a fresh judgment: '+historical['evidence'])
                        except (DispatchError, OSError, ValueError) as error:
                            self.d.db.execute('INSERT OR IGNORE INTO autonomy_seen VALUES (?,?,?,?)',
                                (self.c['id'], 'shadow-error-' + task, self.c['base'], encoded({'error':str(error)[:300]})))
                        break
        pool = self.batch.pool(self.c['pool'])
        if at >= timestamp(self.c['until']) or self.batch.gate(pool['name'], at):
            return {'state':'outside_window'}
        pending = [t for t in snapshot['tasks'] if t['queue_state']=='pending' and not t['dependencies'] and t.get('purpose') not in ('shadow_review','planning')]
        if len(pending) >= self.c['backlog']: return {'state':'backlog_ready'}
        rows = self.d.db.execute("SELECT * FROM autonomy_item WHERE policy=? AND state='pending' ORDER BY rowid", (self.c['id'],)).fetchall()
        if not rows and self.d.db.execute('SELECT count(*) FROM autonomy_item WHERE policy=?',(self.c['id'],)).fetchone()[0] >= self.c['max_tasks']:
            return {'state':'task_budget_reached'}
        if not rows and self.d.db.execute("SELECT 1 FROM autonomy_item a JOIN queued q ON q.task_id=a.task_id WHERE a.policy=? AND a.source='planner' AND q.state IN ('pending','active','continuing')",(self.c['id'],)).fetchone():
            return {'state':'awaiting_proposals'}
        if not rows:
            # A new delivery completion can trigger another bounded proposal pass;
            # clock ticks, planning completions and unchanged blocks cannot.
            progress = sorted((r['task_id'], r['final_commit']) for r in snapshot['ready_receipts']
                              if r.get('purpose', 'delivery')=='delivery')
            key = 'propose-' + digest(progress)[:20]
            if not self.d.db.execute('SELECT 1 FROM autonomy_item WHERE policy=? AND key=?', (self.c['id'],key)).fetchone():
                self.add({'key':key,'scope':next(iter(self.c['scopes'])),'kind':'offline',
                          'objective':'PLANNING ONLY: inspect the pinned roadmap and completed task records. Propose independent implementation, portable-regression or release-engineering follow-ups within the authorized scopes. Read existing evidence first; do not reopen closed gates, repeat reports, launch discovery or create work merely to fill capacity. Return an empty tasks list and precise reason if no useful unblocked work remains.',
                          'references':['PLAN.md','HANDOVER.md'],'dependencies':[]}, 'planner')
                rows=self.d.db.execute("SELECT * FROM autonomy_item WHERE policy=? AND key=?", (self.c['id'],key)).fetchall()
        for row in rows[:max(0,self.c['backlog']-len(pending))]:
            task='auto-'+digest([self.c['id'],row['key']])[:24]
            detail={}
            try:
                worker=self.worker(snapshot)
                if worker:
                    name,reg=worker
                    self.save(row['key'],'approving',{'registry':name},task)
                    self.d.approve(name,reg['session_id'],reg['worktree'],reg['connection'])
                    reg=json.loads(self.d.db.execute('SELECT snapshot FROM registry WHERE name=?',(name,)).fetchone()[0])
                else:
                    name,reg,detail=self.new_worker(row['key'],task)
                item=json.loads(row['payload'])
                learning={**self.c['learning'],'model':self.c['model']}
                paths=['integration/'+task+'/'+p for p in ['README.md','followups.json','inputs.json','results.json','checks.json','artifact-sha256.json','evidence.tar.gz','candidate-tests.patch','candidate-source.patch','candidate-runner.patch','test_verifier.py','test_runner.py','runner.py','probe.pl']]
                self.save(row['key'],'enqueueing',detail,task)
                self.batch.enqueue(task,name,self.prompt(item,task,row['source']),paths,learning,item['dependencies'],
                                   purpose='planning' if row['source']=='planner' else 'delivery')
                self.save(row['key'],'queued',detail,task)
                snapshot=report(self.d.path)
            except (DispatchError,OSError,ValueError,KeyError,TypeError,subprocess.CalledProcessError) as error:
                self.save(row['key'],'held',{**detail,'error':str(error)[:500]},task)
        states=dict(self.d.db.execute('SELECT state,count(*) FROM autonomy_item WHERE policy=? GROUP BY state',(self.c['id'],)))
        return {'state':'observing','items':states,'authority':'No integration or prerequisite approval'}
