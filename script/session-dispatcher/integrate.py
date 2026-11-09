#!/usr/bin/env python3
"""Freeze task+learning, reuse one structured review, and integrate only approved results."""
import argparse
import fcntl
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from dispatch import Dispatcher, DispatchError, digest, encoded, git, identity, now, require
from lifecycle import Lifecycle
import review


class Integration:
    def __init__(self, dispatcher, *, initialize=True):
        self.d = dispatcher
        self.life = Lifecycle(dispatcher)
        if not initialize:
            require(self.d.db.execute("SELECT 1 FROM sqlite_master WHERE name='integration'").fetchone(),
                    'No integration enrollments; status does not enable this extension')
            return
        with self.lock(), self.d.atomic():
            self.d.db.execute("""CREATE TABLE IF NOT EXISTS integration (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES task(id), request TEXT NOT NULL,
                worktree TEXT NOT NULL, branch_key TEXT NOT NULL, target TEXT NOT NULL,
                stage TEXT NOT NULL, snapshot TEXT, approval TEXT, receipt TEXT,
                created TEXT NOT NULL, updated TEXT NOT NULL)""")
            for name, field in [('unit','task_id'),('source','worktree'),('branch','branch_key')]:
                self.d.db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS integration_{name} ON integration({field}) WHERE stage NOT IN ('merged','deferred')")
            self.d.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS integration_target ON integration(target) WHERE stage IN ('approved','merging','uncertain')")
            self.d.db.execute(f"PRAGMA user_version={max(5, self.d.db.execute('PRAGMA user_version').fetchone()[0])}")

    @contextmanager
    def lock(self):
        with self.d.path.open('rb') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DispatchError('Another lifecycle/integration writer holds the ledger') from None
            yield

    def item(self, integration_id):
        row = self.d.db.execute('SELECT * FROM integration WHERE id=?',(integration_id,)).fetchone()
        require(row is not None,'Unknown integration ID')
        item = dict(row)
        for key in ('request','snapshot','approval','receipt'):
            item[key] = json.loads(item[key]) if item[key] else None
        return item

    def save(self, integration_id, stage, **fields):
        fields = {key:encoded(value) for key,value in fields.items()}
        fields.update(stage=stage, updated=now())
        self.d.db.execute('UPDATE integration SET '+','.join(key+'=?' for key in fields)+' WHERE id=?',(*fields.values(),integration_id))
        return self.item(integration_id)

    def enroll(self, task_id, target, model, risks, evidence):
        require(model and set(risks)<=set(review.RISKS),'Explicit review model and known risks required')
        require(len(evidence.encode())<=65536,'Narrow the reviewed evidence summary')
        task = self.d.task(task_id)
        self.life.unit(task_id)  # Only already-enrolled task/learning units; never adopt an unmanaged session.
        registration = json.loads(task['registry'])
        target = identity(target)
        require(not target['status'] and target['common']==registration['common']
                and target['worktree']!=task['worktree'] and target['branch']!=registration['branch'],
                'Need a clean independent target worktree/branch in the same repository')
        request = {'target':{key:target[key] for key in ('worktree','common','git_dir','branch')},
                   'target_head_at_enrollment':target['head'],
                   'model':model,'risks':sorted(set(risks)),'evidence':evidence}
        integration_id = 'integration_' + digest([task_id,request])[:32]
        with self.lock(), self.d.atomic():
            if self.d.db.execute('SELECT 1 FROM integration WHERE id=?',(integration_id,)).fetchone():
                return self.status(integration_id)
            self.target_available(target, task)
            self.d.db.execute("INSERT INTO integration VALUES (?,?,?,?,?,?,'waiting_task',NULL,NULL,NULL,?,?)",
                (integration_id,task_id,encoded(request),task['worktree'],task['branch_key'],target['worktree'],now(),now()))
        return self.status(integration_id)

    def fresh(self, item):
        unit = self.life.result(item['task_id'])
        source = identity(item['worktree'])
        target = identity(item['target'])
        require(not target['status'] and all(target[key]==value for key,value in item['request']['target'].items()),
                'Target identity/branch/status drift')
        self.target_available(target, self.d.task(item['task_id']))
        for local in (source, target):
            require(not any((Path(local['git_dir']) / name).exists() for name in
                    ('MERGE_HEAD','CHERRY_PICK_HEAD','REVERT_HEAD','rebase-merge','rebase-apply','sequencer')),
                    'An unfinished Git operation requires manual reconciliation')
        return unit,source,target

    def target_available(self, target, task):
        self.d.check_lease(target, task['id'])
        require(not self.d.db.execute("""SELECT 1 FROM task WHERE worktree=?
            AND state NOT IN ('verified','accounted')""", (target['worktree'],)).fetchone(),
            'An unfinished assignment owns the target worktree')
        client = self.d.client_factory(json.loads(task['registry'])['connection'])
        for sid in client.json('GET', '/api/session/active')['data']:
            info = client.json('GET', f'/api/session/{sid}')['data']
            require(str(Path(info['location']['directory']).resolve()) != target['worktree'],
                    'An active native session owns the target worktree')
        for row in self.d.db.execute('SELECT snapshot FROM registry'):
            registered = json.loads(row['snapshot'])
            if registered['worktree'] == target['worktree']:
                require(self.d.client_factory(registered['connection']).idle(registered['session_id']),
                        'Target session has active or pending work')

    def freeze(self, item):
        unit,source,target = self.fresh(item)
        request = item['request']
        require(subprocess.run(['git','-C',target['worktree'],'merge-base','--is-ancestor',
                unit['task']['verification']['base'],target['head']],capture_output=True).returncode==0,
                'Target does not contain the approved task base')
        # merge-tree leaves refs/index/worktree untouched, but may create Git objects.
        # Do not invoke arbitrary configured merge drivers during a preflight.
        custom = subprocess.run(['git','-C',target['worktree'],'config','--get-regexp',r'^merge\..*\.driver$'],capture_output=True)
        require(custom.returncode==1,'Custom merge drivers require separate integration review')
        merged = subprocess.run(['git','-C',target['worktree'],'merge-tree','--write-tree','--name-only',
                                 target['head'],source['head']],capture_output=True,text=True)
        require(merged.returncode==0,'Merge conflict or failed preflight; preserve both branches for review')
        tree = merged.stdout.splitlines()[0]
        require(len(tree) in (40,64) and all(c in '0123456789abcdef' for c in tree),'Invalid merge-tree result')
        context = {'target_head': target['head'], 'target_branch': target['branch'], 'merge_tree': tree,
                   'target_diff_since_task_base': git(target['worktree'], 'diff', '--no-ext-diff', '--no-textconv',
                       '--no-renames', unit['task']['verification']['base'], target['head']).decode(),
                   'proposed_target_diff': git(target['worktree'], 'diff', '--no-ext-diff', '--no-textconv',
                       '--no-renames', target['head'], tree).decode()}
        planned = review.plan(self.d,item['task_id'],request['model'],request['risks'],request['evidence'], integration=context)
        snapshot = {'unit_hash':planned['unit_hash'],'review':planned,'context':context,
                    'source':source,'target':target,'tree':tree}
        return self.save(item['id'],'await_review',snapshot=snapshot)

    def checked(self, item):
        unit,source,target = self.fresh(item)
        expected = item['snapshot']
        planned = review.plan(self.d,item['task_id'],item['request']['model'],item['request']['risks'],
                              item['request']['evidence'], integration=expected['context'])
        require(planned['unit_hash']==expected['unit_hash'] and source==expected['source'] and target==expected['target'],
                'Source unit or target advanced; defer and prepare a fresh integration plan')
        return unit,source,target

    def reviewed(self, item):
        if not self.d.db.execute("SELECT 1 FROM sqlite_master WHERE name='review'").fetchone():return None
        row = self.d.db.execute('SELECT * FROM review WHERE task_id=? ORDER BY attempt DESC LIMIT 1',(item['id'],)).fetchone()
        if not row:return None
        record = json.loads(row['result'])
        plan, expected = record['plan'], item['snapshot']['review']
        require(all(plan.get(key)==expected.get(key) for key in ('unit_hash','evidence_hash','integration_hash','template_hash','model'))
                and set(plan['risk_flags'])>=set(expected['risk_flags']), 'Saved review concerns different evidence or unit')
        return {**dict(row),'result':record}

    def decision(self, item):
        saved = self.reviewed(item)
        if not saved:return None
        require(saved['state'] in ('reviewed','reviewed_with_risks'),'Review is incomplete, uncertain or requires escalation')
        verdict = saved['result']['assessment']
        require(verdict['assessment']=='routine' and not verdict['issues'] and not verdict['escalation'],
                'Substantive review issues cannot be overridden by merge approval')
        return saved

    def advance(self, integration_id, *, gpu_available=False, run_review=False):
        paid = None
        with self.lock():
            item = self.item(integration_id)
            if item['stage'] in ('merged','deferred','blocked','uncertain'):return self.status(integration_id)
            if item['stage']=='merging':
                return self.public(self.save(integration_id,'uncertain',receipt={'issue':'Interrupted Git merge; reconcile actual history/effects, never retry'}))
            if item['stage']=='waiting_task':
                unit = self.life.advance_locked(item['task_id'],gpu_available)
                if unit['stage']=='needs_review':return self.public(self.save(integration_id,'blocked',receipt={'issue':'Task/learning requires review'}))
                if unit['stage']!='ready':return self.status(integration_id)
                try:item=self.freeze(item)
                except DispatchError as error:return self.public(self.save(integration_id,'blocked',receipt={'issue':str(error)}))
            self.checked(item)
            if item['stage']=='await_review':
                saved=self.reviewed(item)
                if saved is None and run_review:paid=item['snapshot']['review']
                elif saved:
                    try:
                        self.decision(item)
                        self.save(integration_id,'await_approval')
                    except DispatchError:
                        return {**self.status(integration_id),'action':'review/escalation required; no automatic retry',
                                'review_hold': True}
            elif item['stage']=='approved':
                return self.apply_locked(item)
        # SQLite review intent is persisted by the existing once-only runner.
        # Never hold the lifecycle file lock while waiting on paid inference.
        if paid is not None:
            review.run(self.d,integration_id,paid)
            return self.advance(integration_id)  # Reconcile once; never recurse into another paid run.
        return self.status(integration_id)

    def review(self, integration_id, risks, escalate=False):
        with self.lock():
            item = self.item(integration_id)
            require(item['stage'] in ('await_review','await_approval'), 'Freeze the unit before review')
            self.checked(item)
            request = item['request']
            planned = review.plan(self.d, item['task_id'], request['model'],
                sorted(set(risks) | set(item['snapshot']['review']['risk_flags'])), request['evidence'],
                integration=item['snapshot']['context'])
        return review.run(self.d, integration_id, planned, escalate=escalate)

    def approve(self, integration_id, accepted_risks):
        with self.lock():
            item=self.item(integration_id)
            require(item['stage'] in ('await_review','await_approval','approved'),'Unit is not awaiting merge approval')
            self.checked(item)
            saved=self.decision(item)
            require(saved is not None,'A confirmed structured review is required')
            flags=saved['result']['plan']['risk_flags']
            require(set(accepted_risks)==set(flags),'Acknowledge every reviewed risk explicitly; no risk is waived implicitly')
            approval={'snapshot_hash':digest(item['snapshot']),'review_input_hash':saved['input_hash'],
                      'review_attempt':saved['attempt'],'accepted_risks':sorted(accepted_risks),'approved_at':now()}
            return self.public(self.save(integration_id,'approved',approval=approval))

    def apply_locked(self, item):
        _,source,target=self.checked(item)
        saved=self.decision(item)
        require(saved and item['approval']['review_input_hash']==saved['input_hash']
                and item['approval']['review_attempt']==saved['attempt']
                and item['approval']['snapshot_hash']==digest(item['snapshot']), 'Approval/review changed')
        self.save(item['id'],'merging')  # Before Git/hooks: a lost outcome must never cause another merge.
        command=['git','-C',target['worktree'],'merge','--no-ff','--no-edit','-m',
                 'Merge verified task '+item['task_id']+' and learning\n\nOpenCode-Integration: '+item['id'],source['head']]
        try:
            completed=subprocess.run(command,capture_output=True,timeout=120)
            require(completed.returncode==0,'Git merge/hooks failed; preserve the target for inspection')
            actual=identity(target['worktree'])
            require(not actual['status'] and all(actual[key]==target[key] for key in ('worktree','common','git_dir','branch')),
                    'Target changed during integration')
            parents=git(target['worktree'],'show','-s','--format=%P',actual['head']).decode().split()
            already=actual['head']==target['head']
            require(already or parents==[target['head'],source['head']],'Unexpected integration ancestry')
            require(subprocess.run(['git','-C',target['worktree'],'merge-base','--is-ancestor',source['head'],actual['head']],capture_output=True).returncode==0,
                    'Source commits are not reachable from the integration')
            require(git(target['worktree'],'rev-parse',actual['head']+'^{tree}').decode().strip()==item['snapshot']['tree'],
                    'Actual merged tree differs from preflight')
            receipt={'commit':actual['head'],'target_before':target['head'],'source_tip':source['head'],
                     'tree':item['snapshot']['tree'],'review_input_hash':saved['input_hash'],
                     'task_id':item['task_id'],'already_integrated':already,'verified_at':now()}
            return self.public(self.save(item['id'],'merged',receipt=receipt))
        except (DispatchError,OSError,subprocess.TimeoutExpired) as error:
            return self.public(self.save(item['id'],'uncertain',receipt={'issue':str(error),'automatic_retry':False}))

    def defer(self, integration_id, reason):
        require(reason.strip(),'Record a reason for deferring this unit')
        with self.lock():
            item=self.item(integration_id)
            require(item['stage'] not in ('merging','uncertain','merged'),
                    'Do not release an uncertain or completed merge reservation')
            # Holding the same lock proves no gate merge intent was admitted yet.
            # Keep any revoked approval as evidence, rather than deleting its record.
            return self.public(self.save(integration_id,'deferred',receipt={'reason':reason}))

    def public(self, item):
        planned=item['snapshot']['review'] if item['snapshot'] else None
        latest = None
        if self.d.db.execute("SELECT 1 FROM sqlite_master WHERE name='review'").fetchone():
            row = self.d.db.execute('SELECT * FROM review WHERE task_id=? ORDER BY attempt DESC LIMIT 1',
                                    (item['id'],)).fetchone()
            if row:
                latest = {key: row[key] for key in ('attempt','state','input_hash')}
                latest['effort'] = json.loads(row['result'])['plan']['effort']
        return {'id':item['id'],'task_id':item['task_id'],'stage':item['stage'],'target':item['target'],
                'source_tip':item['snapshot']['source']['head'] if item['snapshot'] else None,
                'review_effort':planned['effort'] if planned else None,'review_input_hash':planned['input_hash'] if planned else None,
                'latest_review':latest,'receipt':item['receipt'],'approval':item['approval'],
                'human_integration_approval_required':True}

    def status(self, integration_id):
        return self.public(self.item(integration_id))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    enroll = commands.add_parser('enroll')
    enroll.add_argument('task_id')
    enroll.add_argument('--target', required=True)
    enroll.add_argument('--model', required=True)
    enroll.add_argument('--evidence', required=True)
    enroll.add_argument('--risk', choices=sorted(review.RISKS), action='append', default=[])
    for name in ('status','advance','review','approve','defer','watch'):
        sub = commands.add_parser(name)
        sub.add_argument('integration_id', nargs='+' if name == 'watch' else None)
        if name in ('advance','watch'):
            sub.add_argument('--gpu-available', action='store_true')
            sub.add_argument('--run-review', action='store_true')
        if name == 'approve':
            sub.add_argument('--accept-risk', action='append', choices=sorted(review.RISKS), default=[])
        if name == 'review':
            sub.add_argument('--risk', action='append', choices=sorted(review.RISKS), default=[])
            sub.add_argument('--escalate', action='store_true')
        if name == 'defer':
            sub.add_argument('--reason', required=True)
        if name == 'watch':
            sub.add_argument('--seconds', type=int, default=3600)
            sub.add_argument('--interval', type=int, default=5)
    args = parser.parse_args()
    dispatcher = None
    try:
        dispatcher = Dispatcher(args.db)
        gate = Integration(dispatcher, initialize=args.command != 'status')
        if args.command == 'enroll':
            result = gate.enroll(args.task_id, args.target, args.model, args.risk, Path(args.evidence).read_text())
        elif args.command == 'approve':
            result = gate.approve(args.integration_id, args.accept_risk)
        elif args.command == 'defer':
            result = gate.defer(args.integration_id, args.reason)
        elif args.command == 'status':
            result = gate.status(args.integration_id)
        elif args.command == 'review':
            print(json.dumps(gate.review(args.integration_id, args.risk, args.escalate), indent=2))
            return 0
        elif args.command == 'advance':
            result = gate.advance(args.integration_id, gpu_available=args.gpu_available, run_review=args.run_review)
        else:
            require(0 <= args.seconds <= 86400 and 1 <= args.interval <= 60, 'Invalid watch bounds')
            deadline = time.monotonic() + args.seconds
            while True:
                result = [gate.advance(i, gpu_available=args.gpu_available, run_review=args.run_review)
                          for i in args.integration_id]
                print(encoded(result), flush=True)
                if all(i.get('review_hold') or i['stage'] in ('merged','deferred','await_approval','blocked','uncertain')
                       or (i['stage'] == 'await_review' and not args.run_review) for i in result):
                    break
                if time.monotonic() >= deadline:
                    return 2
                time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
            return 0 if all(i['stage'] in ('merged','deferred') for i in result) else 1
        print(json.dumps(result, indent=2))
        return 1 if result['stage'] in ('blocked','uncertain') else 0
    except KeyboardInterrupt:
        print('integration: observer stopped; intents/reservations retained', file=sys.stderr)
        return 130
    except (DispatchError,OSError,ValueError,KeyError,TypeError,sqlite3.Error,subprocess.TimeoutExpired) as error:
        print('integration: ' + str(error), file=sys.stderr)
        return 1
    finally:
        if dispatcher:
            dispatcher.close()


if __name__ == '__main__':
    raise SystemExit(main())
