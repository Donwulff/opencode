#!/usr/bin/env python3
"""Explicit checkpointed continuation of a stopped, enrolled task; never replay its prompt."""
import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import re
import sqlite3
import sys
from string import Template
import uuid

from batch import Batch
from dispatch import Dispatcher, DispatchError, digest, encoded, git, identity, now, require, task_history


def same_model(left, right):
    # Native switchModel treats an omitted variant as "default"; session.get
    # materializes it. Explicit non-default variants must still match exactly.
    return all(left.get(key) == right.get(key) for key in ("providerID", "id")) and left.get("variant", "default") == right.get("variant", "default")


def checkpoint(task, accept_base=None):
    registration = json.loads(task["registry"])
    local = identity(task["worktree"])
    require(all(local[key] == registration[key] for key in ("worktree", "common", "git_dir", "branch")),
            "Worktree identity changed during continuation")
    paths = json.loads(task["allowed"])
    if accept_base is not None:
        require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", accept_base) is not None,
                "Accept-base requires a full immutable commit ID")
        require(local["head"] == accept_base and not local["status"],
                "Accepted baseline requires the exact clean current HEAD")
        require(not any((Path(local["git_dir"]) / name).exists() for name in
                        ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer")),
                "Finish the existing Git operation before accepting a baseline")
        require(git(task["worktree"], "merge-base", registration["head"], accept_base).decode().strip() == registration["head"],
                "Accepted baseline must preserve the original history")
        require(not git(task["worktree"], "diff", "--name-only", registration["head"], accept_base, "--", *paths),
                "Baseline advance changed assigned output paths; review separately")
        commits = []  # Reviewed upstream commits are the new base, never task delivery.
    else:
        commits, _ = task_history(task["worktree"], registration["head"], local["head"], paths, task["id"])
    changed = set(git(task["worktree"], "diff", "--name-only", "--no-renames", "-z", "HEAD").decode().split("\0")[:-1])
    changed.update(git(task["worktree"], "diff", "--cached", "--name-only", "--no-renames", "-z").decode().split("\0")[:-1])
    changed.update(git(task["worktree"], "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")[:-1])
    require(changed <= set(paths), "Partial work includes paths outside the assignment; preserve and review it")
    files = {}
    for name in paths:
        file = Path(task["worktree"]) / name
        require(not file.is_symlink() and file.resolve().is_relative_to(Path(task["worktree"]).resolve()),
                "Linked partial files need manual review")
        if file.exists():
            require(file.is_file() and file.stat().st_nlink == 1, "Partial files must be ordinary unlinked files")
            hashed = hashlib.sha256()
            with file.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hashed.update(chunk)
            files[name] = {"sha256": hashed.hexdigest(), "mode": file.stat().st_mode}
        else:
            files[name] = None
    return {"head": local["head"], "status": local["status"], "files": files, "commits": commits,
            "index_hash": hashlib.sha256(git(task["worktree"], "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv")).hexdigest()}


class Recovery:
    def __init__(self, dispatcher, *, batch=None):
        self.d = dispatcher
        if batch is not None:
            self.batch = batch  # Called under the batch's existing lock; no migration/adoption.
            return
        self.batch = Batch(dispatcher)
        with self.batch.lock(), self.d.atomic():
            self.d.db.execute("""CREATE TABLE IF NOT EXISTS attempt (
                task_id TEXT NOT NULL REFERENCES task(id), number INTEGER NOT NULL,
                previous TEXT NOT NULL, plan TEXT NOT NULL, state TEXT NOT NULL,
                created TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(task_id,number))""")
            self.d.db.execute(f"PRAGMA user_version={max(4, self.d.db.execute('PRAGMA user_version').fetchone()[0])}")

    def plan(self, task_id, model, accept_base=None):
        with self.batch.lock():
            return self.plan_locked(task_id, model, accept_base=accept_base)

    def plan_locked(self, task_id, model, number=None, accept_base=None):
        require(isinstance(model, dict) and set(model) <= {"providerID", "id", "variant"}
                and model.get("providerID") and model.get("id"), "Explicit target model required")
        self.d.reconcile(task_id)
        task = self.d.task(task_id)
        unit = self.batch.life.unit(task_id)
        queue = self.d.db.execute("SELECT * FROM queued WHERE task_id=?", (task_id,)).fetchone()
        require(queue and queue["state"] in ("active", "blocked", "continuing"), "Only enrolled unfinished queued tasks can continue")
        require(unit["stage"] in ("task", "needs_review") and not unit["fork_id"] and not unit["child_id"],
                "Task continuation cannot adopt or restart a learning stage")
        require(task["terminal"] in ("failed", "interrupted") and task["delivered"] and not task["issue"]
                and task["hazard"] == "Execution failed/interrupted; effects may already exist",
                "Need an attributable stopped attempt without history gaps, drift or foreign input")
        registration = json.loads(task["registry"])
        client = self.d.client_factory(registration["connection"])
        self.d.check_binding(registration, client)
        require(client.idle(task["session_id"]) and client.tail(task["session_id"]) == task["cursor"],
                "Session must be idle without pending input/forms and have a settled event tail")
        tools = {}
        tool_event = False
        for event in client.events(task["session_id"], registration["cursor"]):
            kind, data = event.get("type"), event.get("data", {})
            tool_event = tool_event or kind.startswith("session.tool.")
            if kind not in ("session.tool.input.started", "session.tool.called", "session.tool.success", "session.tool.failed"):
                continue
            key = (data["assistantMessageID"], data["id"])
            record = tools.setdefault(key, {"id": data["id"], "assistant": data["assistantMessageID"]})
            if kind == "session.tool.input.started":
                require("name" not in record, "Ambiguous repeated tool identity")
                record["name"] = data["name"]
            elif kind == "session.tool.called":
                record["called"] = data["executed"]
            else:
                require("outcome" not in record, "Conflicting tool outcomes")
                record.update(outcome=kind.rsplit(".", 1)[1], executed=data["executed"], event_id=event["id"])
        require(all("name" in tool and "outcome" in tool for tool in tools.values()),
                "Unfinished/unattributed tools may still have effects; continuation refused")
        require(all(not tool.get("called") or tool["executed"] for tool in tools.values()), "Tool execution evidence is inconsistent")
        baseline = None
        if accept_base is not None:
            require(not tool_event, "Accept-base requires zero recorded tool events, not an effects acknowledgement")
            require(not json.loads(task["payload"])["metadata"].get("continuation_attempt"),
                    "Accept-base applies only to an untouched original attempt")
            require(not self.d.db.execute("SELECT 1 FROM attempt WHERE task_id=? AND state NOT IN ('waiting','checkpoint_blocked','superseded')",
                                          (task_id,)).fetchone(), "An earlier handoff may have effects; baseline acceptance refused")
            files = checkpoint(task, accept_base)
            patch = git(task["worktree"], "diff", "--no-ext-diff", "--no-textconv", "--binary", registration["head"], accept_base)
            baseline = {"previous_base": registration["head"], "accepted_base": accept_base,
                        "diff_sha256": hashlib.sha256(patch).hexdigest(),
                        "changed_paths": git(task["worktree"], "diff", "--name-only", registration["head"], accept_base).decode().splitlines(),
                        "commits": git(task["worktree"], "rev-list", "--reverse", registration["head"] + ".." + accept_base).decode().splitlines()}
        else:
            files = checkpoint(task)
        # Executed tools are deliberately not classified from names or command text.
        # Their external/background effects require a checkpoint-specific operator check.
        effects = bool(files["commits"] or files["status"] or any(tool["executed"] for tool in tools.values()))
        target_pool = self.batch.matching_pool(registration["connection"], model["providerID"])
        if number is None:
            last = self.d.db.execute("SELECT number,state FROM attempt WHERE task_id=? ORDER BY number DESC LIMIT 1", (task_id,)).fetchone()
            require(not last or last["state"] == "observing" or
                    (baseline is not None and last["state"] in ("waiting", "checkpoint_blocked")),
                    "An earlier continuation intent remains unresolved; inspect status")
            number = last["number"] + 1 if last else 1
        namespace = self.d.db.execute("SELECT value FROM meta WHERE key='namespace'").fetchone()[0]
        message_id = "msg_continue_" + uuid.uuid5(uuid.UUID(namespace), f"{task_id}:{number}").hex
        template = Path(__file__).with_name("templates").joinpath("continue.txt").read_text()
        stable = {key: value for key, value in task.items() if key != "updated"}
        approval = {"task_id": task_id, "number": number, "previous_hash": digest(stable),
                    "previous_message_id": task["message_id"], "model": model, "pool": target_pool,
                    "checkpoint": files, "cursor": task["cursor"], "tools": list(tools.values()),
                    "effects_check_required": effects, "message_id": message_id, "template_hash": digest(template)}
        if baseline is not None:
            approval["baseline_approval"] = baseline
        return {**approval, "approval_hash": digest(approval)}

    def prepare(self, task_id, model, approval_hash, effects_checked=False, accept_base=None):
        with self.batch.lock():
            previous = self.d.db.execute("SELECT * FROM attempt WHERE task_id=? ORDER BY number DESC LIMIT 1", (task_id,)).fetchone()
            if previous and json.loads(previous["plan"])["approval_hash"] == approval_hash:
                require(json.loads(previous["plan"])["model"] == model, "Approval names another target model")
                require(json.loads(previous["plan"]).get("baseline_approval", {}).get("accepted_base") == accept_base,
                        "Approval names another accepted baseline")
                return {"task_id": task_id, "number": previous["number"], "state": previous["state"]}
            require(not previous or previous["state"] == "observing" or
                    (accept_base is not None and previous["state"] in ("waiting", "checkpoint_blocked")),
                    "An earlier continuation intent remains unresolved")
            plan = self.plan_locked(task_id, model, accept_base=accept_base)
            require(plan["approval_hash"] == approval_hash, "Checkpoint changed; obtain a fresh continuation plan")
            require(not plan["effects_check_required"] or effects_checked,
                    "Executed tools/partial work need an explicit checkpoint-specific --effects-checked acknowledgement")
            queue = dict(self.d.db.execute("SELECT * FROM queued WHERE task_id=?", (task_id,)).fetchone())
            task, unit = self.d.task(task_id), self.batch.life.unit(task_id)
            spec = json.loads(queue["spec"])
            template = Path(__file__).with_name("templates").joinpath("continue.txt").read_text()
            prompt = Template(template).substitute(task_id=task_id, attempt=plan["number"], worktree=task["worktree"],
                base=json.loads(task["registry"])["head"], head=plan["checkpoint"]["head"], paths=task["allowed"],
                checkpoint=digest(plan["checkpoint"]), assignment=spec["text"])
            if accept_base is not None:
                prompt = ("The coordinator explicitly approved the forward repository baseline " + accept_base +
                          " after verifying zero tool events and a clean worktree. Original pinned inputs/scope stay unchanged; "
                          "upstream commits are not task results.\n\n" + prompt)
            payload = {"id": plan["message_id"], "text": prompt, "delivery": "queue", "resume": True,
                       "metadata": {"dispatcher_task": task_id, "continuation_attempt": plan["number"],
                                    "previous_message_id": task["message_id"], "checkpoint_hash": digest(plan["checkpoint"]),
                                    "template_hash": plan["template_hash"], "prompt_hash": digest(prompt),
                                    "base_commit": accept_base or json.loads(task["registry"])["head"]}}
            plan.update(payload=payload, effects_checked=bool(effects_checked))
            with self.d.atomic():
                if previous and previous["state"] in ("waiting", "checkpoint_blocked"):
                    self.save(task_id, previous["number"], "superseded")
                self.d.db.execute("INSERT INTO attempt VALUES (?,?,?,?,'waiting',?,?)", (task_id, plan["number"],
                    encoded({"task": task, "unit": unit, "queued": queue}), encoded(plan), now(), now()))
                self.batch.save(task_id, "continuing")
            return {"task_id": task_id, "number": plan["number"], "state": "waiting"}

    def advance_locked(self, task_id, at=None):
        at = at or datetime.now(timezone.utc)
        row = self.d.db.execute("SELECT * FROM attempt WHERE task_id=? ORDER BY number DESC LIMIT 1", (task_id,)).fetchone()
        require(row is not None, "No continuation intent")
        number, plan = row["number"], json.loads(row["plan"])
        if row["state"] == "observing":
            return
        if row["state"] == "sending":
            # Admission may have occurred before observer death. Only recover its evidence.
            self.d.reconcile(task_id)
            if self.d.task(task_id)["message_id"] == plan["message_id"] and self.d.task(task_id)["enqueued"]:
                self.save(task_id, number, "observing")
                self.batch.save(task_id, "active")
                return
        if row["state"] != "waiting":
            self.save(task_id, number, "blocked")
            self.batch.save(task_id, "blocked", "Interrupted continuation mutation; inspect effects, no automatic retry")
            return
        try:
            accept_base = plan.get("baseline_approval", {}).get("accepted_base")
            current = self.plan_locked(task_id, plan["model"], number, accept_base)
            require(current["approval_hash"] == plan["approval_hash"], "Continuation checkpoint changed while waiting")
            if self.batch.availability(plan["pool"], at, _owner=task_id):
                return
            previous = json.loads(row["previous"])["task"]
            registration = json.loads(previous["registry"])
            client = self.d.client_factory(registration["connection"])
            cursor = plan["cursor"]
            model_events = []
            selected = registration["model"]
            if not same_model(registration["model"], plan["model"]):
                self.save(task_id, number, "switching")
                client.json("POST", f"/api/session/{previous['session_id']}/model", {"model": plan["model"]})
                selected = client.json("GET", f"/api/session/{previous['session_id']}")["data"]["model"]
                require(same_model(selected, plan["model"]), "Selected model/variant differs from the authorized target")
                target = {**registration, "model": selected}
                self.d.check_binding(target, client)
                for event in client.events(previous["session_id"], cursor):
                    if event.get("type") == "log.synced":
                        require(event.get("aggregateID") == previous["session_id"] and event.get("seq") == cursor,
                                "Model handoff event watermark mismatch")
                        continue
                    require(event["type"] == "session.model.selected" and same_model(event["data"]["model"], plan["model"])
                            and event["data"]["sessionID"] == previous["session_id"]
                            and event["durable"]["aggregateID"] == previous["session_id"]
                            and event["durable"]["seq"] == cursor + 1 and not model_events,
                            "Unexpected activity during model handoff")
                    cursor += 1
                    model_events.append(event)
                require(model_events and client.idle(previous["session_id"]) and client.tail(previous["session_id"]) == cursor,
                        "Model handoff is not settled")
            # No tool/filesystem mutation has been authorized during the handoff.
            require(checkpoint(previous, accept_base) == plan["checkpoint"], "Worktree changed during handoff")
            require(client.idle(previous["session_id"]) and client.tail(previous["session_id"]) == cursor,
                    "Session changed before continuation admission")
            registration.update(model=selected, cursor=cursor)
            if accept_base is not None:
                registration["head"] = accept_base
            spec = json.loads(self.d.db.execute("SELECT spec FROM queued WHERE task_id=?", (task_id,)).fetchone()[0])
            spec["task_pool"] = plan["pool"]
            with self.d.atomic():
                for event in model_events:
                    self.d.db.execute("INSERT INTO observed VALUES (?,?,?,?,?)", (task_id, event["durable"]["seq"],
                        event["id"], event["type"], digest(event)))
                self.d.update(task_id, message_id=plan["message_id"], registry=encoded(registration), payload=encoded(plan["payload"]),
                    cursor=cursor, state="sending", enqueued=0, delivered=0, run_start=None, delivery_run=None,
                    terminal=None, hazard=None, issue=None, verification=None)
                self.d.db.execute("UPDATE queued SET spec=? WHERE task_id=?", (encoded(spec), task_id))
                self.d.db.execute("UPDATE unit SET stage='task',detail=?,updated=? WHERE task_id=?",
                                  (encoded({"continuation_attempt": number}), now(), task_id))
                self.save(task_id, number, "sending")
            try:
                result = client.json("POST", f"/api/session/{previous['session_id']}/prompt", plan["payload"])["data"]
                require(result["id"] == plan["message_id"] and result["sessionID"] == previous["session_id"]
                        and result["payload"]["text"] == plan["payload"]["text"], "Continuation admission response mismatch")
                self.d.update(task_id, state="admitted")
            except (DispatchError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
                self.d.update(task_id, state="uncertain", issue="Continuation admission unconfirmed; reconcile without resending")
            self.save(task_id, number, "observing")
            self.batch.save(task_id, "active")
        except (DispatchError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
            state = self.d.db.execute("SELECT state FROM attempt WHERE task_id=? AND number=?", (task_id, number)).fetchone()[0]
            self.save(task_id, number, "checkpoint_blocked" if state == "waiting" else "blocked")
            self.batch.save(task_id, "blocked", str(error))

    def save(self, task_id, number, state):
        self.d.db.execute("UPDATE attempt SET state=?,updated=? WHERE task_id=? AND number=?", (state, now(), task_id, number))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "prepare", "status"):
        sub = commands.add_parser(name)
        sub.add_argument("task_id")
        if name != "status":
            sub.add_argument("--model", required=True, help="Explicit model reference JSON on the same host")
            sub.add_argument("--accept-base", help="Explicitly reapprove this exact clean forward HEAD for a task with zero tool events")
        if name == "prepare":
            sub.add_argument("--approve", required=True, help="Exact approval_hash returned by plan")
            sub.add_argument("--effects-checked", action="store_true", help="Confirm this checkpoint's tool/background/external effects are settled")
    args = parser.parse_args()
    dispatcher = None
    try:
        dispatcher = Dispatcher(args.db)
        if args.command == "status":
            print(json.dumps(dispatcher.status(args.task_id), indent=2))
            return 0  # Inspection alone must not opt an older ledger into recovery.
        recovery = Recovery(dispatcher)
        if args.command == "plan":
            result = recovery.plan(args.task_id, json.loads(args.model), args.accept_base)
        else:
            result = recovery.prepare(args.task_id, json.loads(args.model), args.approve, args.effects_checked, args.accept_base)
        print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        print("recovery: stopped; durable intents remain held, no automatic retry", file=sys.stderr)
        return 130
    except (DispatchError, OSError, ValueError, KeyError, TypeError, sqlite3.Error, http.client.HTTPException) as error:
        print(f"recovery: {error}", file=sys.stderr)
        return 1
    finally:
        if dispatcher:
            dispatcher.close()


if __name__ == "__main__":
    raise SystemExit(main())
