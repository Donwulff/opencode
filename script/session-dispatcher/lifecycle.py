#!/usr/bin/env python3
"""Opt-in post-task learning on the existing dispatcher ledger; never merges branches."""
import argparse
from datetime import datetime, timezone
import fcntl
import http.client
import json
from pathlib import Path
import re
import sqlite3
from string import Template
import time

from dispatch import Dispatcher, DispatchError, allowed_paths, digest, encoded, git, identity, now, require


def snapshot_policy(policy, worktree):
    require(set(policy) <= {"prompt", "model", "allow", "max_active", "wait_seconds"}, "Unknown learning policy key")
    paths = allowed_paths(policy.get("allow", ["AGENTS.md"]))
    for name in paths:
        file = Path(worktree) / name
        require(file.name == "AGENTS.md" and file.is_file() and not file.is_symlink()
                and file.resolve().is_relative_to(Path(worktree)) and file.stat().st_nlink == 1,
                "Learning scope must contain existing, non-linked AGENTS.md files")
        require(git(worktree, "ls-files", "--error-unmatch", "--", name), "Learning file must be tracked")
    model = policy["model"]
    require(set(model) <= {"providerID", "id", "variant"} and model.get("providerID") and model.get("id"), "Explicit learning model required")
    text = Path(policy["prompt"]).read_text()
    require(0 < len(text.encode()) <= 65536, "Learn prompt must contain 1..65536 bytes")
    if text.startswith("---\n"):
        pieces = text.split("\n---\n", 1)
        require(len(pieces) == 2, "Invalid command frontmatter")
        text = pieces[1]
    # This is a literal prompt snapshot, not the command processor: never expand shell or file references.
    text = text.replace("$ARGUMENTS", "")
    require(not re.search(r"^\s*!`", text, re.M), "Executable command expansion is not supported")
    maximum = policy.get("max_active", 3)
    wait = policy.get("wait_seconds", 21600)
    require(type(maximum) is int and 1 <= maximum <= 3, "Learning concurrency must be 1..3")
    require(type(wait) is int and 0 <= wait <= 86400, "GPU wait must be 0..86400 seconds")
    wrapper = Path(__file__).with_name("templates").joinpath("learn.txt").read_text()
    return {"model": model, "allow": paths, "max_active": maximum, "wait_seconds": wait,
            "prompt": text, "prompt_hash": digest(text), "wrapper": wrapper, "wrapper_hash": digest(wrapper)}


class Lifecycle:
    def __init__(self, dispatcher):
        self.d = dispatcher

    def unit(self, task_id):
        row = self.d.db.execute("SELECT * FROM unit WHERE task_id=?", (task_id,)).fetchone()
        require(row is not None, "Task was not enrolled at submission; existing live tasks are not adopted")
        unit = dict(row)
        for key in ("config", "detail"):
            unit[key] = json.loads(unit[key])
        return unit

    def save(self, task_id, stage, **detail):
        unit = self.unit(task_id)
        self.d.db.execute("UPDATE unit SET stage=?,detail=?,updated=? WHERE task_id=?",
                          (stage, encoded({**unit["detail"], **detail}), now(), task_id))
        return self.unit(task_id)

    def clean(self, task):
        registration = json.loads(task["registry"])
        client = self.d.client_factory(registration["connection"])
        local = self.d.check_binding(registration, client)
        require(client.idle(task["session_id"]), "Original task session is no longer idle")
        client.viewed_suffix(task["session_id"], task["cursor"])
        require(not local["status"], "Worktree is dirty")
        return client, local

    def advance(self, task_id, gpu_available=False):
        # A second watcher may inspect status, but must not account/commit a unit concurrently.
        with self.d.path.open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self.unit(task_id)
            return self.advance_locked(task_id, gpu_available)

    def advance_locked(self, task_id, gpu_available):
        unit = self.unit(task_id)
        if unit["stage"] in ("ready", "needs_review"):
            return unit
        try:
            return self._advance(task_id, gpu_available)
        except (DispatchError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
            return self.save(task_id, "needs_review", issue=str(error), automatic_retry=False)

    def _advance(self, task_id, gpu_available):
        unit = self.unit(task_id)
        task = self.d.task(task_id)
        config = unit["config"]
        if unit["stage"] == "task":
            status = self.d.reconcile(task_id)
            if status["state"] not in ("completed", "verified"):
                if status["hazard"] or status["terminal"]:
                    return self.save(task_id, "needs_review", issue="Task execution needs review; no learning started")
                return unit
            status = self.d.verify(task_id, identity(task["worktree"])["head"])
            require(status["state"] == "verified", "Task result is not verified")
            unit = self.save(task_id, "await_gpu", task_commit=status["verification"]["commit"], waiting_since=now())
            task = self.d.task(task_id)
        if unit["stage"] == "await_gpu":
            client, local = self.clean(task)
            require(local["head"] == unit["detail"]["task_commit"], "Task HEAD drift")
            waited = (datetime.now(timezone.utc) - datetime.fromisoformat(unit["detail"]["waiting_since"])).total_seconds()
            if not gpu_available:
                if waited >= config["wait_seconds"]:
                    return self.save(task_id, "ready", learning="skipped_gpu_unavailable", final_commit=local["head"])
                return unit
            # Admission is cooperative and bounded, not a GPU-utilization detector. Count live
            # service work plus this ledger's reservations, including queued/unknown forks.
            busy = set()
            for sid in client.json("GET", "/api/session/active")["data"]:
                info = client.json("GET", f"/api/session/{sid}")["data"]
                if info["model"]["providerID"] == config["model"]["providerID"]:
                    busy.add(sid)
            with self.d.atomic():
                require(self.unit(task_id)["stage"] == "await_gpu", "Another observer claimed this unit")
                for held in self.d.db.execute("SELECT * FROM unit WHERE stage IN ('forking','configuring','learning','accounting')"):
                    if json.loads(held["config"])["model"]["providerID"] == config["model"]["providerID"]:
                        busy.add(held["fork_id"] or held["task_id"])
                if len(busy) >= config["max_active"]:
                    if waited >= config["wait_seconds"]:
                        return self.save(task_id, "ready", learning="skipped_gpu_capacity", final_commit=local["head"])
                    return unit
                self.save(task_id, "forking", learning="pending")
            # Native fork has no client-supplied ID. Persist intent first, issue exactly once.
            fork = client.json("POST", f"/api/session/{task['session_id']}/fork", {})["data"]
            require(fork.get("fork", {}).get("sessionID") == task["session_id"], "Unexpected fork provenance")
            require(fork["location"]["directory"] == local["worktree"], "Fork placement mismatch")
            child_id = "learn_" + digest(task_id)[:32]
            with self.d.atomic():
                self.d.db.execute("UPDATE unit SET fork_id=?,child_id=? WHERE task_id=?", (fork["id"], child_id, task_id))
                self.save(task_id, "configuring")
            # Only this disposable fork is configured; original agent/model/permissions remain unchanged.
            permissions = [{"action": "*", "resource": "*", "effect": "deny"},
                           {"action": "read", "resource": "*", "effect": "allow"}]
            permissions += [{"action": "edit", "resource": name, "effect": "allow"} for name in config["allow"]]
            client.json("PATCH", f"/api/session/{fork['id']}", {"title": f"Learn: {task_id}", "permissions": permissions})
            client.json("POST", f"/api/session/{fork['id']}/model", {"model": config["model"]})
            unit = self.save(task_id, "learning", learning="running")
        if unit["stage"] in ("forking", "configuring", "accounting"):
            # Interrupted mutation may have taken effect. Never automatically repeat it.
            return self.save(task_id, "needs_review", issue="Interrupted fork/configuration/commit; inspect effects before recovery")
        if unit["stage"] == "learning":
            child_id, sid = unit["child_id"], unit["fork_id"]
            registration = json.loads(task["registry"])
            if not self.d.db.execute("SELECT 1 FROM task WHERE id=?", (child_id,)).fetchone():
                self.clean(task)
                self.d.approve(child_id, sid, task["worktree"], registration["connection"], _unit=task_id)
                prompt = Template(config["wrapper"]).substitute(task_id=child_id, parent_id=task_id,
                    worktree=task["worktree"], base=unit["detail"]["task_commit"], paths=encoded(config["allow"]),
                    assignment=config["prompt"], prompt_hash=config["prompt_hash"])
                self.d.submit(child_id, child_id, config["prompt"], config["allow"], _unit=task_id, _prompt=prompt,
                              _template_hash=digest([config["wrapper_hash"], config["prompt_hash"]]))
            self.d.reconcile(child_id)
            child = self.d.task(child_id)
            client = self.d.client_factory(registration["connection"])
            if not child["terminal"] or not client.idle(sid):
                if child["hazard"] or child["issue"]:
                    return self.save(task_id, "needs_review", issue="Learning effects/observation uncertain; no resubmission")
                return self.unit(task_id)
            require(child["delivered"] and child["enqueued"], "No attributable learning delivery")
            require(not child["issue"] and child["hazard"] in (None, "Execution failed/interrupted; effects may already exist"),
                    "Learning history has unexplained changes/gaps")
            require(client.tail(sid) == child["cursor"], "Learning session changed while accounting")
            # All three native writers share permission action "edit". Detect successful use
            # of a different writer from durable events; permissions cannot enforce its name.
            names, wrong_writer = {}, False
            for event in client.events(sid, json.loads(child["registry"])["cursor"]):
                data = event.get("data", {})
                if event.get("type") == "session.tool.input.started":
                    names[data["id"]] = data["name"]
                if (event.get("type") == "session.tool.success" and data.get("executed") is True
                        and names.get(data["id"]) in ("write", "patch", "apply_patch")):
                    wrong_writer = True
                    self.d.db.execute("INSERT OR IGNORE INTO feedback VALUES (?,?,?,?)",
                        (child_id, event["id"], "non_edit_writer_succeeded", json.loads(child["payload"])["metadata"]["template_hash"]))
            # Check the original without requiring clean status: only scoped learning edits may remain.
            self.d.check_binding(registration, client)
            require(client.idle(task["session_id"]), "Original session changed while learning")
            client.viewed_suffix(task["session_id"], task["cursor"])
            local = self.d.check_binding(json.loads(child["registry"]), client)
            require(local["head"] == unit["detail"]["task_commit"], "Learning agent changed HEAD")
            require(not git(task["worktree"], "diff", "--cached", "--name-only"), "Learning agent staged changes")
            require(not git(task["worktree"], "ls-files", "--others", "--exclude-standard", "-z"), "Untracked learning artifacts")
            changes = git(task["worktree"], "diff", "--name-only", "--no-renames", "-z").decode().split("\0")[:-1]
            require(set(changes) <= set(config["allow"]), "Learning changed paths outside its scope")
            for name in changes:
                file = Path(task["worktree"]) / name
                require(file.is_file() and not file.is_symlink() and file.stat().st_nlink == 1,
                        "Learning deleted/linked an instruction file")
            outcome = ("failed_tool_policy" if wrong_writer else "completed" if child["terminal"] == "succeeded"
                       else child["terminal"])
            commit = None
            self.save(task_id, "accounting", learning=outcome, changed_paths=changes)
            if changes:
                # No hooks, staging or commit effects are retried after a crash. A partial learning
                # proposal is preserved with an explicit failure label, never called a successful task.
                message = (f"docs: learning proposal for {task_id} ({outcome})\n\n"
                           f"OpenCode-Task: {child_id}\nOpenCode-Learn: {task_id}\nLearning-Outcome: {outcome}")
                git(task["worktree"], "add", "--", *changes)
                git(task["worktree"], "commit", "--only", "-m", message, "--", *changes)
                commit = identity(task["worktree"])["head"]
                require(git(task["worktree"], "show", "-s", "--format=%P", commit).decode().split() == [local["head"]],
                        "Learning commit parent mismatch")
                require(set(git(task["worktree"], "diff", "--name-only", "--no-renames", local["head"], commit).decode().splitlines()) == set(changes),
                        "Learning commit scope mismatch")
            final = identity(task["worktree"])
            require(not final["status"], "Worktree changed during accounting")
            proof = ({"kind": "git_scope", "base": local["head"], "commit": commit,
                      "tree": git(task["worktree"], "rev-parse", commit + "^{tree}").decode().strip(),
                      "paths": changes, "verified_at": now(), "human_review_required": True,
                      "non_git_effects": "not established by Git verification"} if commit else None)
            # Retain history/evidence but remove the disposable fork from normal session lists.
            archived = True
            try:
                client.json("PATCH", f"/api/session/{sid}", {"archived": True})
            except DispatchError:
                archived = "unconfirmed"  # Archival is housekeeping, not evidence of tool completion.
            with self.d.atomic():
                self.d.update(child_id, state="accounted", verification=encoded(proof) if proof else None,
                              issue="Learning result accounted separately; not a verified task commit")
                return self.save(task_id, "ready", learning=outcome if changes else outcome + "_no_changes",
                                 learning_commit=commit, final_commit=final["head"], fork_archived=archived, human_review_required=True)
        return self.unit(task_id)

    def result(self, task_id):
        unit = self.unit(task_id)
        require(unit["stage"] == "ready", "Integration unit is not ready; task proof remains available through status")
        task = self.d.task(task_id)
        client, local = self.clean(task)
        require(local["head"] == unit["detail"]["final_commit"], "Integration unit HEAD drift")
        feedback = [dict(row) for row in self.d.db.execute("SELECT * FROM feedback WHERE task_id IN (?,?)", (task_id, unit["child_id"]))]
        return {"task": self.d.status(task_id), "learning": unit,
                "learning_task": self.d.status(unit["child_id"]) if unit["child_id"] else None, "feedback": feedback,
                "human_integration_approval_required": True,
                "post_verification_views": client.viewed_suffix(task["session_id"], task["cursor"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("feedback", help="Summarize native failure references by category and template hash")
    for name in ("advance", "status", "result", "watch"):
        sub = commands.add_parser(name)
        sub.add_argument("task_id", nargs="+" if name == "watch" else None)
        if name in ("advance", "watch"):
            sub.add_argument("--gpu-available", action="store_true", help="Operator permits this learning batch to use GPU")
        if name == "watch":
            sub.add_argument("--seconds", type=int, default=3600)
            sub.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    dispatcher = None
    try:
        dispatcher = Dispatcher(args.db)
        lifecycle = Lifecycle(dispatcher)
        if args.command == "feedback":
            groups = []
            for row in dispatcher.db.execute("SELECT category,template_hash,COUNT(*) events,COUNT(DISTINCT task_id) tasks FROM feedback GROUP BY category,template_hash ORDER BY events DESC"):
                refs = [dict(ref) for ref in dispatcher.db.execute("SELECT task_id,event_id FROM feedback WHERE category=? AND template_hash=? LIMIT 5", (row["category"], row["template_hash"]))]
                groups.append({**dict(row), "examples": refs})
            print(json.dumps(groups, indent=2))
            return 0
        if args.command == "watch":
            require(1 <= args.interval <= 60 and 0 <= args.seconds <= 86400, "Invalid watch bounds")
            deadline = time.monotonic() + args.seconds
            while True:
                results = [lifecycle.advance(task, args.gpu_available) for task in args.task_id]
                print(encoded(results), flush=True)
                if all(unit["stage"] in ("ready", "needs_review") for unit in results) or time.monotonic() >= deadline:
                    break
                time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
            if any(unit["stage"] == "needs_review" for unit in results):
                return 1
            return 0 if all(unit["stage"] == "ready" for unit in results) else 2
        result = (lifecycle.unit(args.task_id) if args.command == "status" else
                  lifecycle.result(args.task_id) if args.command == "result" else
                  lifecycle.advance(args.task_id, args.gpu_available))
        print(json.dumps(result, indent=2))
        return 1 if result.get("stage") == "needs_review" else 0
    except (DispatchError, OSError, ValueError, KeyError, TypeError, sqlite3.Error, http.client.HTTPException) as error:
        print(f"lifecycle: {error}", file=__import__("sys").stderr)
        return 1
    finally:
        if dispatcher:
            dispatcher.close()


if __name__ == "__main__":
    raise SystemExit(main())
