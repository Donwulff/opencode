"""Immutable queue prerequisites in the existing ledger; no selection, retries or merges."""
import json
import re
import subprocess

from dispatch import DispatchError, digest, encoded, now, require


def normalize(values):
    require(isinstance(values, list) and len(values) <= 128, "Dependencies must be a list of at most 128 edges")
    result = []
    for value in values:
        require(isinstance(value, dict) and set(value) <= {"task", "when", "ref"}, "Invalid dependency fields")
        task, condition = value.get("task"), value.get("when")
        require(isinstance(task, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task), "Invalid prerequisite task ID")
        require(condition in ("verified", "integrated", "approved"), "Unknown dependency condition")
        item = {"task": task, "when": condition}
        if condition == "integrated":
            ref = value.get("ref", "refs/heads/main")
            require(isinstance(ref, str) and ref.startswith("refs/heads/")
                    and subprocess.run(["git", "check-ref-format", ref], capture_output=True).returncode == 0,
                    "Integration dependency requires a local branch ref")
            item["ref"] = ref
        else:
            require("ref" not in value, "Only integration dependencies accept a branch ref")
        result.append(item)
    result.sort(key=encoded)
    require(len({encoded(item) for item in result}) == len(result), "Duplicate dependency")
    return result


class Dependencies:
    def __init__(self, dispatcher):
        self.d = dispatcher

    def exists(self):
        return self.d.db.execute("SELECT 1 FROM sqlite_master WHERE name='queue_dependency'").fetchone() is not None

    def add_locked(self, task_id, values):
        """Caller holds the queue lock and transaction; migration is opt-in on first edge."""
        self.d.db.execute("""CREATE TABLE IF NOT EXISTS queue_dependency (
            task_id TEXT NOT NULL REFERENCES queued(task_id), prerequisite_id TEXT NOT NULL,
            requirement TEXT NOT NULL CHECK(requirement IN ('verified','integrated','approved')),
            target_ref TEXT NOT NULL, approval TEXT, admission TEXT,
            PRIMARY KEY(task_id,prerequisite_id,requirement,target_ref))""")
        graph = {}
        for owner, prerequisite in self.d.db.execute("SELECT task_id,prerequisite_id FROM queue_dependency"):
            graph.setdefault(owner, set()).add(prerequisite)
        graph[task_id] = {item['task'] for item in values}
        pending, seen = list(graph[task_id]), set()
        while pending:
            current = pending.pop()
            require(current != task_id, "Circular dependency")
            if current not in seen:
                seen.add(current)
                pending.extend(graph.get(current, ()))
        for item in values:
            self.d.db.execute("INSERT INTO queue_dependency VALUES (?,?,?,?,NULL,NULL)",
                (task_id, item['task'], item['when'], item.get('ref', '')))
        self.d.db.execute("PRAGMA user_version=6")  # Older dispatchers must refuse this ledger.

    def edges(self, task_id):
        queued = self.d.db.execute("SELECT * FROM queued WHERE task_id=?", (task_id,)).fetchone()
        if queued is None:
            return None, []
        spec = json.loads(queued['spec'])
        expected = spec.get('dependencies', [])
        rows = [dict(row) for row in self.d.db.execute("SELECT * FROM queue_dependency WHERE task_id=?", (task_id,))] if self.exists() else []
        actual = [{'task': row['prerequisite_id'], 'when': row['requirement'],
                   **({'ref': row['target_ref']} if row['requirement'] == 'integrated' else {})} for row in rows]
        require(sorted(actual, key=encoded) == expected, "Frozen dependency definition changed; review required")
        return dict(queued), sorted(rows, key=lambda row: encoded([row['prerequisite_id'], row['requirement'], row['target_ref']]))

    def result(self, task_id):
        queued = self.d.db.execute("SELECT state FROM queued WHERE task_id=?", (task_id,)).fetchone()
        if queued and queued['state'] in ('blocked', 'deferred'):
            return 'blocked', 'Prerequisite is ' + queued['state'], None
        if queued and queued['state'] == 'continuing':
            return 'waiting', 'Approved prerequisite continuation is pending', None
        task = self.d.db.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        if task is None:
            return 'waiting', 'Prerequisite has no verified delivery yet', None
        if task['issue'] or task['hazard'] or task['terminal'] in ('failed', 'interrupted'):
            return 'blocked', 'Prerequisite has unresolved execution/effects', None
        if task['state'] != 'verified':
            return ('blocked' if task['state'] == 'accounted' else 'waiting'), 'Verified delivery required', None
        require(task['verification'], "Verified prerequisite lacks its immutable receipt")
        proof = json.loads(task['verification'])
        unit = self.d.db.execute("SELECT stage,detail FROM unit WHERE task_id=?", (task_id,)).fetchone()
        if unit and unit['stage'] != 'ready':
            return ('blocked' if unit['stage'] == 'needs_review' else 'waiting'), 'Learning unit is not accounted', None
        detail = json.loads(unit['detail']) if unit else {}
        final = detail.get('final_commit') if unit else proof['commit']
        require(isinstance(final, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', final), "Missing final integration-unit commit")
        basis = {'task': task_id, 'verification': proof, 'final_commit': final,
                 'learning': detail.get('learning')}
        return 'satisfied', '', {'fingerprint': digest(basis), 'commit': proof['commit'], 'final_commit': final}

    def states(self, task_id):
        queued, rows = self.edges(task_id)
        values = []
        for row in rows:
            item = {'task': row['prerequisite_id'], 'when': row['requirement']}
            if row['target_ref']:
                item['ref'] = row['target_ref']
            # Once dispatched, retain admission-time facts, not mutable source worktrees.
            if queued['state'] != 'pending' and row['admission']:
                values.append({**item, **json.loads(row['admission'])})
                continue
            state, reason, basis = self.result(row['prerequisite_id'])
            if state == 'satisfied' and row['requirement'] == 'integrated':
                task = self.d.task(row['prerequisite_id'])
                common = json.loads(task['registry'])['common']
                git = ['git', '--git-dir', common]
                head = subprocess.run(git + ['rev-parse', '--verify', row['target_ref'] + '^{commit}'], capture_output=True, text=True)
                if head.returncode:
                    state, reason = 'waiting', 'Integration target ref unavailable'
                else:
                    head = head.stdout.strip()
                    ancestor = subprocess.run(git + ['merge-base', '--is-ancestor', basis['final_commit'], head], capture_output=True)
                    if ancestor.returncode:
                        state, reason = ('waiting', 'Final task/learning commit not integrated') if ancestor.returncode == 1 else ('blocked', 'Git integration evidence unavailable')
                    else:
                        basis = {**basis, 'target_commit': head, 'ref': row['target_ref']}
            if state == 'satisfied' and row['requirement'] == 'approved':
                approval = json.loads(row['approval']) if row['approval'] else None
                if not approval or approval['fingerprint'] != basis['fingerprint'] or approval['request_hash'] != queued['request_hash']:
                    state, reason = 'waiting', 'Explicit dependency approval required'
                else:
                    basis = {**basis, 'approval_hash': digest(approval)}
            values.append({**item, 'state': state, 'reason': reason, 'proof': basis})
        return values

    def approve_locked(self, task_id, prerequisite, reason):
        require(reason.strip() and len(reason.encode()) <= 4096, "Supply a bounded approval reason")
        queued, rows = self.edges(task_id)
        require(queued and queued['state'] == 'pending'
                and not self.d.db.execute('SELECT 1 FROM task WHERE id=?', (task_id,)).fetchone(),
                "Only an unsubmitted pending dependency can be approved")
        rows = [row for row in rows if row['prerequisite_id'] == prerequisite and row['requirement'] == 'approved']
        require(len(rows) == 1, "No matching explicit-approval dependency")
        state, _, proof = self.result(prerequisite)
        require(state == 'satisfied', "Approval cannot replace verified delivery and learning accounting")
        approval = {'fingerprint': proof['fingerprint'], 'request_hash': queued['request_hash'], 'reason': reason}
        if rows[0]['approval']:
            old = json.loads(rows[0]['approval'])
            require(all(old[key] == value for key, value in approval.items()), "Approval changed; defer and enqueue a new task ID")
            return old
        approval['approved_at'] = now()
        self.d.db.execute("UPDATE queue_dependency SET approval=? WHERE task_id=? AND prerequisite_id=? AND requirement='approved'",
                          (encoded(approval), task_id, prerequisite))
        return approval

    def admit_locked(self, task_id):
        states = self.states(task_id)
        require(all(item['state'] == 'satisfied' for item in states), "Prerequisite is not satisfied")
        for item in states:
            self.d.db.execute("UPDATE queue_dependency SET admission=? WHERE task_id=? AND prerequisite_id=? AND requirement=? AND target_ref=? AND admission IS NULL",
                (encoded({**item, 'admitted_at': now()}), task_id, item['task'], item['when'], item.get('ref', '')))

    def require_admitted(self, task_id, assignment):
        queued, rows = self.edges(task_id)
        if not rows:
            return []
        require(queued['state'] == 'active' and all(row['admission'] for row in rows), "Submit dependent tasks through the queue admission gate")
        spec = json.loads(queued['spec'])
        require(all(spec[key] == value for key, value in assignment.items()), "Queued assignment changed before submission")
        return [{key: value for key, value in json.loads(row['admission']).items() if key in ('task', 'when', 'ref', 'proof')}
                for row in rows]
