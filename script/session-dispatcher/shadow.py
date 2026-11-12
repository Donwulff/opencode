#!/usr/bin/env python3
"""Advisory calibration on the existing queue; never grants review or merge approval."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

from batch import Batch
from dispatch import Dispatcher, DispatchError, digest, git, require
from operations import report, review_ready
from review import RISKS
from runner import read_json, validate_directory, write_json


def policy(path):
    p = Path(path).resolve(strict=True)
    require(not p.stat().st_mode & 0o077, 'Shadow policy must be private')
    c = read_json(p)
    require(c.get('version') == 1 and type(c['max_cases']) is int and 1 <= c['max_cases'] <= 10
            and type(c['max_disagreements']) is int and 1 <= c['max_disagreements'] <= 3, 'Bounded shadow trial required')
    root = Path(c['root']).resolve(strict=True)
    validate_directory(c['database'], root)
    require(not root.stat().st_mode & 0o077 and not root.is_relative_to(Path(c['repository']).resolve()),
            'Keep shadow receipts private and outside the project')
    return c


def assessment(v):
    require(isinstance(v, dict) and set(v) == {'assessment', 'issues', 'escalation'}
            and v['assessment'] in ('routine', 'needs_escalation', 'blocked')
            and isinstance(v['issues'], list) and all(isinstance(x, str) for x in v['issues'])
            and isinstance(v['escalation'], list) and all(isinstance(x, str) and x in RISKS for x in v['escalation']),
            'Invalid advisory result')
    return v


class Shadow:
    def __init__(self, dispatcher, config):
        self.d, self.c, self.root = dispatcher, config, Path(config['root'])
        self.batch = Batch(dispatcher)

    def cases(self):
        return [read_json(p) for p in sorted(self.root.glob('case-*.json'))]

    def save(self, case):
        write_json(self.root / ('case-' + case['id'] + '.json'), case)
        return case

    @contextmanager
    def lock(self):
        fd = os.open(self.root/'writer.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'r+') as handle:
            require(not os.fstat(handle.fileno()).st_mode & 0o077,'Shadow writer lock must be private')
            try: fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise DispatchError('Another shadow writer owns the trial') from None
            yield

    def capture(self, task_id, *, target=None, kind='forward'):
        with self.lock(): return self._capture(task_id,target=target,kind=kind)

    def _capture(self, task_id, *, target=None, kind='forward'):
        require(kind in ('forward', 'cold_replay'), 'Unknown shadow mode')
        receipt = next((r for r in report(self.c['database'])['ready_receipts'] if r['task_id'] == task_id), None)
        require(receipt and receipt.get('purpose') != 'shadow_review' and receipt['verified_delivery']
                and receipt['proof_kind'] == 'git_scope', 'Verified delivery required; never shadow a shadow')
        require(receipt['common'] == git(self.c['repository'],'rev-parse','--path-format=absolute','--git-common-dir').decode().strip(),
                'Shadow receipt belongs to another project')
        case_id = digest([task_id, receipt['final_commit']])[:24]
        existing = next((x for x in self.cases() if x['id'] == case_id), None)
        if existing:
            return existing  # Interrupted setup stays held: no filesystem/native-effect replay.
        require(len(self.cases()) < self.c['max_cases'], 'Shadow case budget reached')
        require(sum(bool(x.get('disagreement')) for x in self.cases()) < self.c['max_disagreements'], 'Shadow disagreement stop reached')
        repo = self.c['repository']
        target = git(repo, 'rev-parse', '--verify', (target or self.c['target_ref']) + '^{commit}').decode().strip()
        sid, task = 'ses_shadow' + case_id, 'shadow-' + case_id
        directory = self.root / case_id
        case = {'id':case_id, 'source_task':task_id, 'source_commit':receipt['final_commit'], 'target_commit':target,
                'kind':kind, 'session_id':sid, 'shadow_task':task, 'directory':str(directory),
                'stage':'preparing', 'advisory_only':True}
        self.save(case)  # Durable intent BEFORE local preparation and the one native session-create POST.
        directory.mkdir(mode=0o700)
        total, pins, exported = 0, {}, set()
        def put(name, data):
            nonlocal total
            rel = PurePosixPath(name)
            require(not rel.is_absolute() and '..' not in rel.parts, 'Unsafe frozen evidence path')
            total += len(data)
            require(total <= 32*1024*1024 and len(data) <= 8*1024*1024, 'Narrow oversized evidence; never truncate it')
            p = directory / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            pins[name] = hashlib.sha256(data).hexdigest()
        paths = git(repo, 'diff', '--name-only', '-z', receipt['base'], receipt['final_commit']).decode().split('\0')[:-1]
        for phase, base, tip in [('task',receipt['base'],receipt['task_commit']), ('learning',receipt['task_commit'],receipt['final_commit'])]:
            put('patches/'+phase+'.diff', git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', base, tip))
        pending = [(p,0) for p in paths]
        missing = []
        while pending:
            path, depth = pending.pop(0)
            if path in exported: continue
            require(len(exported) < 256, 'Narrow the shadow reference closure')
            result = subprocess.run(['git','-C',repo,'show',receipt['final_commit']+':'+path], capture_output=True)
            if result.returncode:
                missing.append(path); continue
            exported.add(path)
            put('evidence/'+path,result.stdout)
            if path.endswith('.md') and depth < 1:
                for link in re.findall(r'\]\(([^)#]+)(?:#[^)]*)?\)', result.stdout.decode(errors='replace')):
                    if ':' in link or link.startswith('/'): continue
                    parts = list(PurePosixPath(path).parent.parts)
                    for part in PurePosixPath(link).parts:
                        if part == '..':
                            if parts: parts.pop()
                        elif part != '.': parts.append(part)
                    pending.append(('/'.join(parts),depth+1))
            if path.endswith('.tar.gz'):
                with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
                    for m in archive:
                        if m.isfile():
                            require(m.size <= 8*1024*1024, 'Oversized archive member')
                            put('raw/'+digest(path)[:12]+'/'+m.name.removeprefix('./'),archive.extractfile(m).read())
        for name in ['AGENTS.md','PLAN.md','HANDOVER.md']:
            r = subprocess.run(['git','-C',repo,'show',target+':'+name],capture_output=True)
            if r.returncode == 0: put('target/'+name,r.stdout)
        put('AGENTS.md',b'Advisory shadow repository. Inspect only frozen local inputs.\nWrite result.json and review.md, then run python3 finish.py.\nNo network, containers, historical command execution or live author state.\nNo acceptance authority. Dispatcher owns separate learning.\n')
        put('finish.py',Path(__file__).with_name('shadow_finish.py').read_bytes())
        body = {'receipt':receipt,'target_commit':target,'kind':kind,'shadow_task':task,'risks':list(RISKS),'files':pins,
                'missing_reference_paths':sorted(set(missing)), 'scope':'Integrating this unit as a record is distinct from release approval.',
                'limits':'Bounded frozen reference closure; state missing context. Reference judgment is excluded.'}
        write_json(directory/'input.json',body)
        case['input_hash'] = digest(body)
        subprocess.run(['git','init','-q','-b','shadow-'+case_id,str(directory)],check=True,capture_output=True)
        with (directory/'.git/config').open('a') as f:
            f.write('\n[user]\n name = Dispatcher Shadow\n email = shadow@localhost.invalid\n[commit]\n gpgsign = false\n[core]\n hooksPath = /dev/null\n')
        subprocess.run(['git','-C',str(directory),'add','--all'],check=True)
        subprocess.run(['git','-C',str(directory),'commit','-q','-m','Frozen shadow inputs'],check=True)
        case.update(stage='creating_session',base=git(directory,'rev-parse','HEAD').decode().strip())
        self.save(case)
        permissions = [{'action':'*','resource':'*','effect':'deny'}, {'action':'read','resource':'*','effect':'allow'},
                       {'action':'glob','resource':'*','effect':'allow'}, {'action':'grep','resource':'*','effect':'allow'},
                       {'action':'edit','resource':'result.json','effect':'allow'}, {'action':'edit','resource':'review.md','effect':'allow'},
                       {'action':'shell','resource':'python3 finish.py','effect':'allow'}]
        client = self.d.client_factory(self.c['connection'])
        session = client.json('POST','/api/session',{'id':sid,'title':'Shadow: '+task_id,'agent':'build',
            'location':{'directory':str(directory)},'model':self.c['model'],'permissions':permissions})['data']
        require(session['id']==sid and not session.get('fork') and not session.get('parentID'), 'Fresh independent reviewer required')
        case['stage']='approving';self.save(case)
        self.d.approve(task,sid,directory,self.c['connection'])
        prompt = Path(__file__).with_name('templates').joinpath('shadow.txt').read_text()
        case.update(stage='enqueueing',template_hash=digest(prompt));self.save(case)
        self.batch.enqueue(task,task,prompt,['result.json','review.md'],self.c['learning'],purpose='shadow_review')
        case['stage']='queued'
        return self.save(case)

    def reference(self, case_id, input_hash, verdict, evidence):
        with self.lock(): return self._reference(case_id,input_hash,verdict,evidence)

    def _reference(self, case_id, input_hash, verdict, evidence):
        case = next((x for x in self.cases() if x['id']==case_id),None)
        require(case and case['input_hash']==input_hash, 'Reference must match exact frozen inputs')
        require(verdict in ('routine','needs_escalation','blocked') and evidence.strip() and len(evidence.encode())<=65536,
                'Explicit bounded reference decision/evidence required')
        ref = {'input_hash':input_hash,'assessment':verdict,'evidence':evidence,'actor_thread':os.environ.get('CODEX_THREAD_ID'),
               'target_at_decision':git(self.c['repository'],'rev-parse',self.c['target_ref']).decode().strip()}
        require(not case.get('reference') or case['reference']==ref, 'Reference is immutable')
        case['reference']=ref
        return self.save(case)

    def collect(self):
        with self.lock(): return self._collect()

    def _collect(self):
        for case in self.cases():
            row = self.d.db.execute('SELECT state FROM queued WHERE task_id=?',(case['shadow_task'],)).fetchone()
            if not row or row[0]!='ready': continue
            unit = self.d.db.execute('SELECT detail FROM unit WHERE task_id=?',(case['shadow_task'],)).fetchone()
            task = self.d.task(case['shadow_task']);proof = json.loads(task['verification']);tip = proof['commit']
            value = assessment(json.loads(git(case['directory'],'show',tip+':result.json')))
            require(digest(json.loads(git(case['directory'],'show',proof['base']+':input.json')))==case['input_hash'], 'Input drift')
            case.update(stage='complete',result=value,result_commit=tip,learning=json.loads(unit[0]).get('learning'),
                        model=json.loads(task['registry'])['model'])
            if case.get('reference'):
                case['disagreement']=value['assessment']!=case['reference']['assessment']
                case['comparison']='classification agreement only; not correctness or acceptance'
            self.save(case)
        return self.cases()


def cycle(path):
    c = policy(path);d = Dispatcher(c['database'])
    try:
        s = Shadow(d,c);s.collect()
        if len(s.cases())<c['max_cases'] and sum(bool(x.get('disagreement')) for x in s.cases())<c['max_disagreements']:
            for unit in review_ready(c['database'],c['repository'],c['target_ref'])['units']:
                if unit['state']=='awaiting_review' and not any(x['source_task']==unit['task_id'] for x in s.cases()):
                    s.capture(unit['task_id']);break
        return s.cases()
    finally:d.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--policy',required=True);p.add_argument('command',choices=['capture','reference','status','cycle'])
    p.add_argument('--task');p.add_argument('--target');p.add_argument('--cold-replay',action='store_true')
    p.add_argument('--case');p.add_argument('--input-hash');p.add_argument('--assessment',choices=['routine','needs_escalation','blocked'])
    p.add_argument('--evidence-file');a=p.parse_args()
    try:
        c=policy(a.policy)
        if a.command=='cycle':v=cycle(a.policy)
        else:
            d=Dispatcher(c['database'])
            try:
                s=Shadow(d,c)
                if a.command=='capture':v=s.capture(a.task,target=a.target,kind='cold_replay' if a.cold_replay else 'forward')
                elif a.command=='reference':v=s.reference(a.case,a.input_hash,a.assessment,Path(a.evidence_file).read_text())
                else:v=s.collect()
            finally:d.close()
        print(json.dumps(v,indent=2));return 0
    except (DispatchError,OSError,ValueError,KeyError,TypeError,subprocess.CalledProcessError,tarfile.TarError) as e:
        print('shadow: '+str(e),file=__import__('sys').stderr);return 1


if __name__=='__main__':raise SystemExit(main())
