#!/usr/bin/env python3
"""Drain explicitly approved tasks through the existing dispatcher and learning lifecycle."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

from dispatch import Dispatcher, DispatchError, allowed_paths, digest, encoded, now, require
from lifecycle import Lifecycle, snapshot_policy
from dependencies import Dependencies, normalize


def timestamp(value):
    stamp = datetime.fromisoformat(value)
    require(stamp.tzinfo is not None, "Availability needs an explicit UTC offset")
    return stamp.astimezone(timezone.utc)


def inference_held(db, row, client):
    # A non-verified task always holds its task/unit/worktree/branch/session evidence
    # in the ledger. Its *inference* reservation additionally ends only for a
    # deliberately deferred, retained stopped attempt that is proven idle: the queued
    # row explicitly in the deferred state (never for ready/active/blocked/absent
    # queue records), terminal failure/interruption with known admission and delivery,
    # no history gap, drift or foreign input, every continuation intent superseded, the
    # session not natively active and without pending input/forms, and the retained
    # tail still at the reconciled cursor (the same settled-tail proof as continuation
    # planning). Missing any of that evidence keeps the conservative reservation. This
    # releases inference capacity only; it never releases the worktree/branch/session
    # guards.
    queued = db.execute("SELECT state FROM queued WHERE task_id=?", (row["id"],)).fetchone()
    if queued is None or queued["state"] != "deferred":
        return True
    if (row["terminal"] not in ("failed", "interrupted") or not row["enqueued"] or not row["delivered"]
            or row["issue"] is not None or row["hazard"] != "Execution failed/interrupted; effects may already exist"):
        return True
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='attempt'").fetchone():
        return True  # Pre-recovery ledgers have no continuation evidence to supersede.
    states = [state for state, in db.execute("SELECT state FROM attempt WHERE task_id=?", (row["id"],))]
    if not states or any(state != "superseded" for state in states):
        return True
    if not client.idle(row["session_id"]):
        return True
    try:
        return client.tail(row["session_id"]) != row["cursor"]
    except DispatchError:
        return True  # An unprovable retained tail keeps the conservative reservation.


def capacity_members(db, pool, client, *, owner=None):
    """Read the same conservative reservations for admission and operational status."""
    busy = {}
    def add(sid, kind, task_id=None, stage=None):
        busy.setdefault(sid, []).append({"kind": kind, "task_id": task_id, "stage": stage})
    for sid in client.json("GET", "/api/session/active")["data"]:
        if client.json("GET", f"/api/session/{sid}")["data"]["model"]["providerID"] == pool["provider"]:
            add(sid, "native_active")
    for row in db.execute("SELECT * FROM task WHERE state NOT IN ('verified','accounted')"):
        if row["id"] == owner:
            continue  # Only an explicitly checked stopped attempt can reuse its own slot.
        binding = json.loads(row["registry"])
        if binding["connection"] == pool["connection"] and binding["model"]["providerID"] == pool["provider"] \
                and inference_held(db, row, client):
            add(row["session_id"], "unresolved_task", row["id"])
    for row in db.execute("""SELECT u.*,t.registry FROM unit u JOIN task t ON t.id=u.task_id
                           WHERE u.stage IN ('forking','configuring','learning','accounting','needs_review')"""):
        policy, binding = json.loads(row["config"]), json.loads(row["registry"])
        if (row["stage"] != "needs_review" or row["fork_id"]) and binding["connection"] == pool["connection"] \
                and policy["model"]["providerID"] == pool["provider"]:
            add(row["fork_id"] or row["task_id"], "learning_unit", row["task_id"], row["stage"])
    return busy


class Batch:
    def __init__(self, dispatcher):
        self.d = dispatcher
        self.life = Lifecycle(dispatcher)
        self.dependencies = Dependencies(dispatcher)
        # Opt-in only: ordinary dispatch/lifecycle use does not migrate a live ledger.
        with self.lock(), self.d.atomic():
            self.d.db.execute("""CREATE TABLE IF NOT EXISTS pool (
                name TEXT PRIMARY KEY, connection TEXT NOT NULL, provider TEXT NOT NULL,
                capacity INTEGER NOT NULL, external INTEGER NOT NULL, state TEXT NOT NULL,
                until_at TEXT, UNIQUE(connection,provider))""")
            self.d.db.execute("""CREATE TABLE IF NOT EXISTS queued (
                task_id TEXT PRIMARY KEY, spec TEXT NOT NULL, request_hash TEXT NOT NULL,
                state TEXT NOT NULL, issue TEXT, created TEXT NOT NULL, updated TEXT NOT NULL)""")
            self.d.db.execute("PRAGMA user_version=" + str(max(3, self.d.db.execute("PRAGMA user_version").fetchone()[0])))

    @contextmanager
    def lock(self):
        with self.d.path.open("rb") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DispatchError("Another lifecycle/queue writer holds the ledger") from None
            yield

    def pool(self, name):
        row = self.d.db.execute("SELECT * FROM pool WHERE name=?", (name,)).fetchone()
        require(row is not None, "Unknown resource pool")
        return dict(row)

    def configure(self, name, connection, provider, capacity, state, until=None, external=0):
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name), "Invalid pool name")
        require(type(capacity) is int and 1 <= capacity <= 16 and type(external) is int and external >= 0,
                "Invalid pool capacity/external occupancy")
        require(provider and state in ("available", "paused", "quota", "offline", "limited"), "Invalid provider/state")
        require(state != "available" or until, "Available pools require a bounded availability deadline")
        if until:
            until = timestamp(until).isoformat()
        connection = str(Path(connection).resolve(strict=True))
        self.d.client_factory(connection)  # Validate the existing local connection; never start a server.
        with self.lock(), self.d.atomic():
            old = self.d.db.execute("SELECT * FROM pool WHERE name=?", (name,)).fetchone()
            require(not old or (old["connection"], old["provider"]) == (connection, provider), "Pool binding is immutable")
            self.d.db.execute("""INSERT INTO pool VALUES (?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                capacity=excluded.capacity,external=excluded.external,state=excluded.state,until_at=excluded.until_at""",
                (name, connection, provider, capacity, external, state, until))
        return self.pool(name)

    def matching_pool(self, connection, provider):
        row = self.d.db.execute("SELECT name FROM pool WHERE connection=? AND provider=?", (connection, provider)).fetchone()
        require(row is not None, "Configure both task and learning provider pools before enqueueing")
        return row[0]

    def enqueue(self, task_id, name, text, paths, learning, dependencies=None):
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id), "Invalid task ID")
        require(text.strip() and len(text.encode()) <= 65536, "Task text must contain 1..65536 bytes")
        paths = allowed_paths(paths)
        assignment = {"name": name, "text": text, "paths": paths, "learning": learning}
        dependencies = normalize([] if dependencies is None else dependencies)
        if dependencies:
            assignment['dependencies'] = dependencies
        with self.lock(), self.d.atomic():
            old = self.d.db.execute("SELECT * FROM queued WHERE task_id=?", (task_id,)).fetchone()
            if old:
                require(old["request_hash"] == digest(assignment), "Task ID already names another queued assignment")
                return {"task_id": task_id, "state": old["state"]}
            require(not self.d.db.execute("SELECT 1 FROM task WHERE id=?", (task_id,)).fetchone(), "Existing tasks are not adopted")
            row = self.d.db.execute("SELECT snapshot FROM registry WHERE name=?", (name,)).fetchone()
            require(row is not None, "Session is not approved")
            registration = json.loads(row[0])
            policy = snapshot_policy(learning, registration["worktree"])
            task_pool = self.matching_pool(registration["connection"], registration["model"]["providerID"])
            learn_pool = self.matching_pool(registration["connection"], policy["model"]["providerID"])
            spec = {**assignment, "registration": registration, "policy": policy,
                    "task_pool": task_pool, "learn_pool": learn_pool,
                    "template_hash": digest(Path(__file__).with_name("templates").joinpath("task.txt").read_text())}
            # Future dependent bases are not guessed or silently reapproved.
            for queued in self.d.db.execute("SELECT spec FROM queued WHERE state IN ('pending','active','blocked','continuing')"):
                other = json.loads(queued[0])["registration"]
                require(all(registration[key] != other[key] for key in ("session_id", "worktree"))
                        and (registration["common"], registration["branch"]) != (other["common"], other["branch"]),
                        "Another queued assignment reserves this session/worktree/branch")
            self.d.db.execute("INSERT INTO queued VALUES (?,?,?,'pending',NULL,?,?)",
                              (task_id, encoded(spec), digest(assignment), now(), now()))
            if dependencies:
                self.dependencies.add_locked(task_id, dependencies)
        return {"task_id": task_id, "state": "pending"}

    def approve_dependency(self, task_id, prerequisite, reason):
        with self.lock(), self.d.atomic():
            return self.dependencies.approve_locked(task_id, prerequisite, reason)

    def defer_pending(self, task_id, reason):
        require(reason.strip() and len(reason.encode()) <= 4096, "Supply a bounded deferral reason")
        with self.lock(), self.d.atomic():
            row = self.d.db.execute("SELECT state FROM queued WHERE task_id=?", (task_id,)).fetchone()
            require(row and row['state'] == 'pending' and not self.d.db.execute("SELECT 1 FROM task WHERE id=?", (task_id,)).fetchone(),
                    "Only an unsubmitted pending task can be deferred here")
            self.save(task_id, 'deferred', reason)
        return self.status()

    def gate(self, name, at):
        pool = self.pool(name)
        if pool["state"] != "available":
            return pool["state"]
        if not pool["until_at"] or timestamp(pool["until_at"]) <= at:
            return "outside_window"

    def availability(self, name, at, *, _owner=None):
        reason = self.gate(name, at)
        if reason:
            return reason
        pool = self.pool(name)
        client = self.d.client_factory(pool["connection"])
        busy = capacity_members(self.d.db, pool, client, owner=_owner)
        return "capacity" if len(busy) + pool["external"] >= pool["capacity"] else None

    def save(self, task_id, state, issue=None):
        self.d.db.execute("UPDATE queued SET state=?,issue=?,updated=? WHERE task_id=?", (state, issue, now(), task_id))

    def status(self):
        tasks = [dict(row) for row in self.d.db.execute("SELECT task_id,state,issue FROM queued ORDER BY created,task_id")]
        for row in tasks:
            try:
                dependencies = self.dependencies.states(row['task_id'])
                if dependencies:
                    row['dependencies'] = dependencies
            except (DispatchError, OSError, ValueError, KeyError, TypeError) as error:
                row['dependency_error'] = str(error)
        return {"tasks": tasks,
                "pools": [{key: row[key] for key in ("name", "provider", "capacity", "external", "state", "until_at")}
                          for row in self.d.db.execute("SELECT * FROM pool ORDER BY name")],
                "integration": "human_review_required"}

    def pause_limits(self, task_id, spec):
        unit = self.life.unit(task_id)
        for owner, name in ((task_id, spec["task_pool"]), (unit["child_id"], spec["learn_pool"])):
            if owner is None:
                continue
            anchor = json.loads(self.d.task(owner)["registry"])["cursor"]
            categories = {row[0] for row in self.d.db.execute("""SELECT f.category FROM feedback f JOIN observed o
                ON f.task_id=o.task_id AND f.event_id=o.event_id WHERE f.task_id=? AND o.seq>?""", (owner, anchor))}
            state = "quota" if "provider_quota" in categories else "limited" if "provider_rate_limit" in categories else None
            if state:
                self.d.db.execute("UPDATE pool SET state=? WHERE name=?", (state, name))

    def tick(self, at=None):
        at = at or datetime.now(timezone.utc)
        require(at.tzinfo is not None, "Queue clock needs an explicit UTC offset")
        with self.lock():
            if self.d.db.execute("SELECT 1 FROM queued WHERE state='continuing'").fetchone():
                from recovery import Recovery
                recovery = Recovery(self.d, batch=self)
                for row in self.d.db.execute("SELECT task_id FROM queued WHERE state='continuing' ORDER BY created,task_id").fetchall():
                    try:
                        recovery.advance_locked(row[0], at=at)
                    except (DispatchError, OSError, ValueError, KeyError, TypeError) as error:
                        self.save(row[0], 'blocked', str(error))
            # Recover observation only. A claimed assignment with no durable task intent stays held.
            for row in self.d.db.execute("SELECT * FROM queued WHERE state='active' ORDER BY created,task_id").fetchall():
                task_id, spec = row["task_id"], json.loads(row["spec"])
                if not self.d.db.execute("SELECT 1 FROM task WHERE id=?", (task_id,)).fetchone():
                    self.save(task_id, "blocked", "Admission intent interrupted; inspect before recovery")
                    continue
                try:
                    current = self.life.unit(task_id)
                    stage = current["stage"]
                    # A crash can leave a configured fork before its prompt admission.
                    # Observing after cutoff must not start that still-unsubmitted work.
                    if (stage == "learning" and not self.d.db.execute("SELECT 1 FROM task WHERE id=?", (current["child_id"],)).fetchone()
                            and self.gate(spec["learn_pool"], at)):
                        continue
                    gpu = stage == "await_gpu" and self.availability(spec["learn_pool"], at) is None
                    unit = self.life.advance_locked(task_id, gpu)
                    # A just-verified task releases inference capacity. Give its reserved
                    # learning stage first admission before draining more task work.
                    if unit["stage"] == "await_gpu" and self.availability(spec["learn_pool"], at) is None:
                        unit = self.life.advance_locked(task_id, True)
                    self.pause_limits(task_id, spec)
                    if unit["stage"] in ("ready", "needs_review"):
                        self.save(task_id, "ready" if unit["stage"] == "ready" else "blocked", unit["detail"].get("issue"))
                except (DispatchError, OSError, ValueError, KeyError, TypeError) as error:
                    self.save(task_id, "blocked", str(error))
            reasons = {}
            waiting_dependencies = False
            for row in self.d.db.execute("SELECT * FROM queued WHERE state='pending' ORDER BY created,task_id").fetchall():
                task_id, spec = row["task_id"], json.loads(row["spec"])
                try:
                    dependencies = self.dependencies.states(task_id)
                    if any(item['state'] != 'satisfied' for item in dependencies):
                        reasons[task_id] = self.gate(spec['task_pool'], at) or 'dependencies'
                        if reasons[task_id] == 'dependencies' and not any(item['state'] == 'blocked' for item in dependencies):
                            waiting_dependencies = True
                        continue
                    reason = self.availability(spec["task_pool"], at)
                    if reason:
                        reasons[task_id] = reason
                        continue
                    registry = self.d.db.execute("SELECT snapshot FROM registry WHERE name=?", (spec["name"],)).fetchone()
                    require(registry and json.loads(registry[0]) == spec["registration"], "Queued approval changed; review required")
                    require(snapshot_policy(spec["learning"], spec["registration"]["worktree"]) == spec["policy"], "Learning prompt/policy changed")
                    require(digest(Path(__file__).with_name("templates").joinpath("task.txt").read_text()) == spec["template_hash"], "Task template changed")
                    with self.d.atomic():
                        self.dependencies.admit_locked(task_id)
                        self.save(task_id, "active")  # Intent + prerequisite proof before POST; never replay.
                    self.d.submit(task_id, spec["name"], spec["text"], spec["paths"], learning=spec["learning"])
                except (DispatchError, OSError, ValueError, KeyError, TypeError) as error:
                    self.save(task_id, "blocked", str(error))
                    # Only this assignment is held. Existing ownership, inference
                    # reservations and provider limits still fence all other admissions.
            result = self.status()
            pending = [row for row in result["tasks"] if row["state"] in ("pending", "continuing")]
            active = [row for row in result["tasks"] if row["state"] == "active"]
            open_work = False
            for row in self.d.db.execute("SELECT task_id,spec,state FROM queued WHERE state IN ('active','continuing')"):
                spec = json.loads(row["spec"])
                if row["state"] == "continuing":
                    intent = self.d.db.execute("SELECT plan FROM attempt WHERE task_id=? ORDER BY number DESC LIMIT 1", (row["task_id"],)).fetchone()
                    pool = json.loads(intent[0])["pool"]
                    reasons[row["task_id"]] = "continuation_waiting"
                else:
                    pool = spec["task_pool"] if self.life.unit(row["task_id"])["stage"] == "task" else spec["learn_pool"]
                reason = self.gate(pool, at)
                task = self.d.task(row['task_id'])
                if reason is None and (row['state'] == 'continuing' or not task['issue'] and not task['hazard']):
                    open_work = True
                else:
                    reasons[row["task_id"]] = reason or 'observation_uncertain'
            # Provider gates control admission, not the lifetime of observation.
            # Keep accounting active work (including bounded learning wait/skip)
            # without opening a pool, retrying a prompt or cancelling execution.
            held = any(row['state'] == 'blocked' for row in result['tasks'])
            dependency_blocked = any(row.get('dependencies') and any(item['state'] == 'blocked' for item in row['dependencies'])
                                     for row in pending)
            phase = ('running' if open_work else 'draining' if active else 'waiting' if waiting_dependencies
                     else 'blocked' if held or dependency_blocked else 'waiting' if 'capacity' in reasons.values()
                     else 'paused' if pending else 'complete')
            return {**result, "phase": phase, "waiting": reasons}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    pool = commands.add_parser("pool")
    pool.add_argument("name")
    pool.add_argument("--connection", required=True)
    pool.add_argument("--provider", required=True)
    pool.add_argument("--capacity", type=int, required=True)
    pool.add_argument("--external", type=int, default=0)
    pool.add_argument("--state", choices=("available", "paused", "quota", "offline", "limited"), default="paused")
    pool.add_argument("--available-until")
    queue = commands.add_parser("enqueue")
    queue.add_argument("task_id")
    queue.add_argument("--session", required=True)
    queue.add_argument("--task-file", required=True)
    queue.add_argument("--allow", action="append", required=True)
    queue.add_argument("--learning-policy", required=True)
    queue.add_argument("--dependencies", help="JSON list of immutable task-ID prerequisites")
    approve = commands.add_parser('approve-dependency')
    approve.add_argument('task_id')
    approve.add_argument('prerequisite')
    approve.add_argument('--reason', required=True)
    defer = commands.add_parser('defer')
    defer.add_argument('task_id')
    defer.add_argument('--reason', required=True)
    status = commands.add_parser("status")
    status.add_argument("--operations", action="store_true", help="Read-only operational view without reconciliation")
    status.add_argument("--live", action="store_true", help="GET-only native occupancy/form snapshots")
    status.add_argument("--run-dir")
    status.add_argument("--text", action="store_true")
    commands.add_parser("tick")
    run = commands.add_parser("run")
    run.add_argument("--seconds", type=int, default=3600)
    run.add_argument("--interval", type=int, default=5)
    args = parser.parse_args()
    dispatcher = None
    try:
        if args.command == "status" and args.operations:
            from operations import report
            from runner import text_report
            result = report(args.db, live=args.live, run_dir=args.run_dir)
            print(text_report(result) if args.text else json.dumps(result, indent=2))
            return 0
        if args.command == "status":
            require(not (args.live or args.run_dir or args.text), "Use --operations for operational status flags")
        dispatcher = Dispatcher(args.db)
        batch = Batch(dispatcher)
        if args.command == "pool":
            result = batch.configure(args.name, args.connection, args.provider, args.capacity, args.state, args.available_until, args.external)
            result = {key: value for key, value in result.items() if key != "connection"}
        elif args.command == "enqueue":
            result = batch.enqueue(args.task_id, args.session, Path(args.task_file).read_text(), args.allow,
                                   json.loads(Path(args.learning_policy).read_text()),
                                   json.loads(Path(args.dependencies).read_text()) if args.dependencies else None)
        elif args.command == 'approve-dependency':
            result = batch.approve_dependency(args.task_id, args.prerequisite, args.reason)
        elif args.command == 'defer':
            result = batch.defer_pending(args.task_id, args.reason)
        elif args.command == "status":
            result = batch.status()
        elif args.command == "tick":
            result = batch.tick()
        else:
            require(0 <= args.seconds <= 86400 and 1 <= args.interval <= 60, "Invalid run bounds")
            from runner import observer_lock
            with observer_lock(args.db):
                deadline = time.monotonic() + args.seconds
                while True:
                    result = batch.tick()
                    print(encoded(result), flush=True)
                    if result["phase"] not in ("running", "draining", "waiting") or time.monotonic() >= deadline:
                        return 0 if result["phase"] == "complete" else 1 if result["phase"] == "blocked" else 2
                    time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
        print(json.dumps(result, indent=2))
        return 1 if result.get("phase") == "blocked" else 0
    except KeyboardInterrupt:
        print("batch: observer stopped; assignments and reservations retained", file=sys.stderr)
        return 130
    except (DispatchError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(f"batch: {error}", file=sys.stderr)
        return 1
    finally:
        if dispatcher:
            dispatcher.close()


if __name__ == "__main__":
    raise SystemExit(main())
