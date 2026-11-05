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


def timestamp(value):
    stamp = datetime.fromisoformat(value)
    require(stamp.tzinfo is not None, "Availability needs an explicit UTC offset")
    return stamp.astimezone(timezone.utc)


class Batch:
    def __init__(self, dispatcher):
        self.d = dispatcher
        self.life = Lifecycle(dispatcher)
        # Opt-in only: ordinary dispatch/lifecycle use does not migrate a live ledger.
        with self.lock(), self.d.atomic():
            self.d.db.execute("""CREATE TABLE IF NOT EXISTS pool (
                name TEXT PRIMARY KEY, connection TEXT NOT NULL, provider TEXT NOT NULL,
                capacity INTEGER NOT NULL, external INTEGER NOT NULL, state TEXT NOT NULL,
                until_at TEXT, UNIQUE(connection,provider))""")
            self.d.db.execute("""CREATE TABLE IF NOT EXISTS queued (
                task_id TEXT PRIMARY KEY, spec TEXT NOT NULL, request_hash TEXT NOT NULL,
                state TEXT NOT NULL, issue TEXT, created TEXT NOT NULL, updated TEXT NOT NULL)""")
            self.d.db.execute("PRAGMA user_version=3")

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

    def enqueue(self, task_id, name, text, paths, learning):
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id), "Invalid task ID")
        require(text.strip() and len(text.encode()) <= 65536, "Task text must contain 1..65536 bytes")
        paths = allowed_paths(paths)
        assignment = {"name": name, "text": text, "paths": paths, "learning": learning}
        with self.lock():
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
            for queued in self.d.db.execute("SELECT spec FROM queued WHERE state IN ('pending','active','blocked')"):
                other = json.loads(queued[0])["registration"]
                require(all(registration[key] != other[key] for key in ("session_id", "worktree"))
                        and (registration["common"], registration["branch"]) != (other["common"], other["branch"]),
                        "Another queued assignment reserves this session/worktree/branch")
            self.d.db.execute("INSERT INTO queued VALUES (?,?,?,'pending',NULL,?,?)",
                              (task_id, encoded(spec), digest(assignment), now(), now()))
        return {"task_id": task_id, "state": "pending"}

    def gate(self, name, at):
        pool = self.pool(name)
        if pool["state"] != "available":
            return pool["state"]
        if not pool["until_at"] or timestamp(pool["until_at"]) <= at:
            return "outside_window"

    def availability(self, name, at):
        reason = self.gate(name, at)
        if reason:
            return reason
        pool = self.pool(name)
        client = self.d.client_factory(pool["connection"])
        busy = set()
        for sid in client.json("GET", "/api/session/active")["data"]:
            if client.json("GET", f"/api/session/{sid}")["data"]["model"]["providerID"] == pool["provider"]:
                busy.add(sid)
        for row in self.d.db.execute("SELECT session_id,registry FROM task WHERE state NOT IN ('verified','accounted')"):
            binding = json.loads(row["registry"])
            if binding["connection"] == pool["connection"] and binding["model"]["providerID"] == pool["provider"]:
                busy.add(row["session_id"])
        for row in self.d.db.execute("""SELECT u.*,t.registry FROM unit u JOIN task t ON t.id=u.task_id
                                      WHERE u.stage IN ('forking','configuring','learning','accounting','needs_review')"""):
            policy, binding = json.loads(row["config"]), json.loads(row["registry"])
            if (row["stage"] != "needs_review" or row["fork_id"]) and binding["connection"] == pool["connection"] and policy["model"]["providerID"] == pool["provider"]:
                busy.add(row["fork_id"] or row["task_id"])
        return "capacity" if len(busy) + pool["external"] >= pool["capacity"] else None

    def save(self, task_id, state, issue=None):
        self.d.db.execute("UPDATE queued SET state=?,issue=?,updated=? WHERE task_id=?", (state, issue, now(), task_id))

    def status(self):
        return {"tasks": [dict(row) for row in self.d.db.execute(
                    "SELECT task_id,state,issue FROM queued ORDER BY created,task_id")],
                "pools": [{key: row[key] for key in ("name", "provider", "capacity", "external", "state", "until_at")}
                          for row in self.d.db.execute("SELECT * FROM pool ORDER BY name")],
                "integration": "human_review_required"}

    def pause_limits(self, task_id, spec):
        unit = self.life.unit(task_id)
        for owner, name in ((task_id, spec["task_pool"]), (unit["child_id"], spec["learn_pool"])):
            categories = {row[0] for row in self.d.db.execute("SELECT category FROM feedback WHERE task_id=?", (owner,))}
            state = "quota" if "provider_quota" in categories else "limited" if "provider_rate_limit" in categories else None
            if state:
                self.d.db.execute("UPDATE pool SET state=? WHERE name=?", (state, name))

    def tick(self, at=None):
        at = at or datetime.now(timezone.utc)
        require(at.tzinfo is not None, "Queue clock needs an explicit UTC offset")
        with self.lock():
            # Recover observation only. A claimed assignment with no durable task intent stays held.
            for row in self.d.db.execute("SELECT * FROM queued WHERE state='active' ORDER BY created,task_id").fetchall():
                task_id, spec = row["task_id"], json.loads(row["spec"])
                if not self.d.db.execute("SELECT 1 FROM task WHERE id=?", (task_id,)).fetchone():
                    self.save(task_id, "blocked", "Admission intent interrupted; inspect before recovery")
                    continue
                try:
                    stage = self.life.unit(task_id)["stage"]
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
            if self.d.db.execute("SELECT 1 FROM queued WHERE state='blocked'").fetchone():
                return {**self.status(), "phase": "blocked"}
            active = self.d.db.execute("SELECT task_id FROM queued WHERE state='active'").fetchall()
            if any(self.d.task(row[0])["issue"] or self.d.task(row[0])["hazard"] for row in active):
                return {**self.status(), "phase": "blocked", "reason": "Observation/effects uncertain; no new admissions"}
            reasons = {}
            for row in self.d.db.execute("SELECT * FROM queued WHERE state='pending' ORDER BY created,task_id").fetchall():
                task_id, spec = row["task_id"], json.loads(row["spec"])
                try:
                    reason = self.availability(spec["task_pool"], at)
                    if reason:
                        reasons[task_id] = reason
                        continue
                    registry = self.d.db.execute("SELECT snapshot FROM registry WHERE name=?", (spec["name"],)).fetchone()
                    require(registry and json.loads(registry[0]) == spec["registration"], "Queued approval changed; review required")
                    require(snapshot_policy(spec["learning"], spec["registration"]["worktree"]) == spec["policy"], "Learning prompt/policy changed")
                    require(digest(Path(__file__).with_name("templates").joinpath("task.txt").read_text()) == spec["template_hash"], "Task template changed")
                    self.save(task_id, "active")  # Intent before any POST; never automatically repeat an interrupted admission.
                    self.d.submit(task_id, spec["name"], spec["text"], spec["paths"], learning=spec["learning"])
                    if self.d.task(task_id)["issue"] or self.d.task(task_id)["hazard"]:
                        return {**self.status(), "phase": "blocked", "reason": "Admission unconfirmed; reconcile without resending"}
                except (DispatchError, OSError, ValueError, KeyError, TypeError) as error:
                    self.save(task_id, "blocked", str(error))
                    return {**self.status(), "phase": "blocked"}
            result = self.status()
            pending = [row for row in result["tasks"] if row["state"] == "pending"]
            active = [row for row in result["tasks"] if row["state"] == "active"]
            open_work = False
            for row in self.d.db.execute("SELECT task_id,spec FROM queued WHERE state='active'"):
                spec = json.loads(row["spec"])
                pool = spec["task_pool"] if self.life.unit(row["task_id"])["stage"] == "task" else spec["learn_pool"]
                if self.gate(pool, at) is None:
                    open_work = True
            return {**result, "phase": "running" if open_work else "paused" if active or pending else "complete", "waiting": reasons}


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
    commands.add_parser("status")
    commands.add_parser("tick")
    run = commands.add_parser("run")
    run.add_argument("--seconds", type=int, default=3600)
    run.add_argument("--interval", type=int, default=5)
    args = parser.parse_args()
    dispatcher = None
    try:
        dispatcher = Dispatcher(args.db)
        batch = Batch(dispatcher)
        if args.command == "pool":
            result = batch.configure(args.name, args.connection, args.provider, args.capacity, args.state, args.available_until, args.external)
            result = {key: value for key, value in result.items() if key != "connection"}
        elif args.command == "enqueue":
            result = batch.enqueue(args.task_id, args.session, Path(args.task_file).read_text(), args.allow,
                                   json.loads(Path(args.learning_policy).read_text()))
        elif args.command == "status":
            result = batch.status()
        elif args.command == "tick":
            result = batch.tick()
        else:
            require(0 <= args.seconds <= 86400 and 1 <= args.interval <= 60, "Invalid run bounds")
            deadline = time.monotonic() + args.seconds
            while True:
                result = batch.tick()
                print(encoded(result), flush=True)
                if result["phase"] != "running" or time.monotonic() >= deadline:
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
