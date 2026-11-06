#!/usr/bin/env python3
"""Manual V2 session dispatch. The OpenCode event store owns execution history."""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
from string import Template
import urllib.error
import urllib.parse
import urllib.request
import uuid


class DispatchError(Exception):
    pass


class DriftError(DispatchError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise DispatchError(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, connection, timeout=10):
        self.connection = str(Path(connection).resolve())
        file = Path(connection)
        require(not file.is_symlink(), "Connection file must not be a symlink")
        config = json.loads(file.read_text())
        url = urllib.parse.urlsplit(config["url"])
        require(url.scheme == "http" and url.hostname in ("127.0.0.1", "::1", "localhost")
                and not url.username and not url.password and not url.query and not url.fragment
                and url.path in ("", "/"), "Only an explicit loopback HTTP service is supported")
        require(isinstance(config.get("password"), str) and config["password"], "Missing service password")
        require(file.stat().st_mode & 0o077 == 0, "Connection file must be private (chmod 600)")
        self.url = config["url"].rstrip("/")
        self.authorization = "Basic " + base64.b64encode(("opencode:" + config["password"]).encode()).decode()
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def open(self, method, path, payload=None):
        headers = {"Authorization": self.authorization}
        data = None
        if payload is not None:
            data = encoded(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            error.close()
            raise DispatchError(f"API {method} failed: HTTP {error.code}") from None
        except OSError:
            raise DispatchError(f"API {method} connection failed; outcome may be unknown") from None

    def json(self, method, path, payload=None):
        with self.open(method, path, payload) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        require(len(raw) <= 16 * 1024 * 1024, "API response exceeds local read limit")
        return json.loads(raw) if raw else None

    def events(self, session_id, after=None):
        query = {"follow": "false"}
        if after is not None:
            query["after"] = str(after)
        path = f"/api/experimental/session/{session_id}/log?" + urllib.parse.urlencode(query)
        with self.open("GET", path) as response:
            require("text/event-stream" in response.headers.get("Content-Type", ""), "Expected session SSE log")
            parts = []
            size = 0
            while True:
                line = response.readline(8 * 1024 * 1024 + 1)
                if not line:
                    require(not parts, "Observation ended inside an SSE frame")
                    break
                size += len(line)
                require(size <= 8 * 1024 * 1024, "Session event exceeds local frame limit")
                text = line.decode().rstrip("\r\n")
                if text.startswith("data:"):
                    parts.append(text[5:].lstrip())
                elif not text:
                    if parts:
                        yield json.loads("\n".join(parts))
                    parts, size = [], 0

    def idle(self, session_id):
        active = self.json("GET", "/api/session/active")["data"]
        inbox = self.json("GET", f"/api/session/{session_id}/inbox")["data"]
        forms = self.json("GET", f"/api/session/{session_id}/form")["data"]
        return session_id not in active and not inbox and not forms

    def tail(self, session_id):
        last = None
        marker = None
        for event in self.events(session_id):
            if event.get("type") == "log.synced":
                marker = event.get("seq")
                require(event.get("aggregateID") == session_id, "Wrong session watermark")
            else:
                require(event["durable"]["aggregateID"] == session_id, "Wrong session event")
                last = event["durable"]["seq"]
        require(marker is not None and last == marker,
                "Current event tail is not retained; launch with events.persist=true")
        return marker

    def viewed_suffix(self, session_id, cursor):
        """Validate read acknowledgements without changing the frozen task proof."""
        tail, marker, views = cursor, None, []
        for event in self.events(session_id, cursor):
            require(marker is None, "Events followed the replay watermark")
            if event.get("type") == "log.synced":
                require(event.get("aggregateID") == session_id, "Wrong session watermark")
                marker = event.get("seq")
                continue
            durable, data = event.get("durable", {}), event.get("data", {})
            require(durable.get("aggregateID") == session_id and data.get("sessionID") == session_id,
                    "Wrong session in post-verification replay")
            seq, event_id = durable.get("seq"), event.get("id")
            require(type(seq) is int and seq == tail + 1 and isinstance(event_id, str)
                    and event_id.startswith("evt_") and event_id not in {view["id"] for view in views},
                    "Post-verification event gap or conflicting replay")
            require(event.get("type") == "session.viewed", "Original session advanced after verification")
            views.append({"id": event_id, "seq": seq, "hash": digest(event)})
            tail = seq
        require(type(marker) is int and marker == tail,
                "Post-verification replay is incomplete; retained events are required")
        return {"cursor": cursor, "tail": tail, "views": views}


def git(path, *args):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "log.showSignature=false",
                             "-C", str(path), *args], env=env, capture_output=True)
    require(result.returncode == 0, f"Git inspection failed ({args[0]})")
    return result.stdout


def identity(worktree):
    path = Path(worktree).resolve(strict=True)
    top = Path(git(path, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    require(path == top, "Register the worktree root, not a subdirectory")
    common = Path(git(path, "rev-parse", "--git-common-dir").decode().strip())
    return {"worktree": str(path), "common": str((path / common).resolve()),
            "git_dir": git(path, "rev-parse", "--absolute-git-dir").decode().strip(),
            "branch": git(path, "symbolic-ref", "--quiet", "HEAD").decode().strip(),
            "head": git(path, "rev-parse", "HEAD").decode().strip(),
            "status": git(path, "status", "--porcelain=v1", "--untracked-files=all").decode()}


def session_binding(info):
    require(not info.get("parentID"), "Only independent root sessions are supported")
    require(not info["location"].get("workspaceID"), "Only local directory placement is supported")
    return {"session_id": info["id"], "project_id": info["projectID"], "agent": info["agent"],
            "model": info["model"], "directory": str(Path(info["location"]["directory"]).resolve())}


def allowed_paths(paths):
    result = sorted(set(paths))
    require(result, "At least one exact allowed file path is required")
    for name in result:
        path = PurePosixPath(name)
        require(name and not path.is_absolute() and ".." not in path.parts and path.parts
                and path.parts[0] != ".git" and path.as_posix() == name and "\n" not in name and "\0" not in name,
                "Allowed paths must be normalized repository-relative file names")
    return result


def task_history(worktree, base, head, paths, task_id):
    """Verify each preserved linear task commit, including changes later reverted."""
    commits = git(worktree, "rev-list", "--reverse", base + ".." + head).decode().splitlines()
    previous, changed = base, set()
    for commit in commits:
        require(git(worktree, "show", "-s", "--format=%P", commit).decode().split() == [previous],
                "Task history is not a linear descendant of its approved base")
        names = git(worktree, "diff", "--name-only", "--no-renames", "-z", previous, commit).decode().split("\0")[:-1]
        require(set(names) <= set(paths), "Preserved commit changed paths outside the assignment")
        require(f"OpenCode-Task: {task_id}" in git(worktree, "show", "-s", "--format=%B", commit).decode().splitlines(),
                "Preserved commit lacks the exact task trailer")
        changed.update(names)
        previous = commit
    require(previous == head, "Task HEAD does not descend from its approved base")
    return commits, sorted(changed)


class Dispatcher:
    def __init__(self, database, client_factory=Client):
        self.path = Path(database).absolute()
        require(not self.path.is_symlink(), "Ledger must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        require(self.path.stat().st_mode & 0o077 == 0, "Ledger must be private (chmod 600)")
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        application_id = self.db.execute("PRAGMA application_id").fetchone()[0]
        tables = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone()
        if application_id not in (0, 0x4F434450) or (tables and application_id != 0x4F434450):
            self.db.close()
            raise DispatchError("Not a dispatcher ledger; refusing to modify this database")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5):
            self.db.close()
            raise DispatchError("Unsupported dispatcher ledger version")
        self.db.execute("PRAGMA application_id=1329808464")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS registry (name TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS task (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL, registry_name TEXT NOT NULL,
            registry TEXT NOT NULL, worktree TEXT NOT NULL, branch_key TEXT NOT NULL,
            payload TEXT NOT NULL, request_hash TEXT NOT NULL, allowed TEXT NOT NULL,
            state TEXT NOT NULL, cursor INTEGER NOT NULL, enqueued INTEGER NOT NULL DEFAULT 0,
            delivered INTEGER NOT NULL DEFAULT 0, run_start INTEGER, delivery_run INTEGER,
            terminal TEXT, hazard TEXT, issue TEXT, verification TEXT,
            created TEXT NOT NULL, updated TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS observed (
            task_id TEXT NOT NULL REFERENCES task(id), seq INTEGER NOT NULL,
            event_id TEXT NOT NULL, type TEXT NOT NULL, hash TEXT NOT NULL,
            PRIMARY KEY(task_id, seq), UNIQUE(task_id,event_id));
        """)
        # Version 2 clients hold worktrees through learning, including after task verification.
        # An older client must refuse this ledger rather than bypass those reservations.
        with self.atomic():
            if version < 2:
                for name, column in (("active_session", "registry_name"), ("active_session_id", "session_id"),
                                     ("active_worktree", "worktree"), ("active_branch", "branch_key")):
                    self.db.execute("DROP INDEX IF EXISTS " + name)
                    self.db.execute(f"CREATE UNIQUE INDEX {name} ON task({column}) WHERE state NOT IN ('verified','accounted')")
            self.db.execute("""CREATE TABLE IF NOT EXISTS unit (
                task_id TEXT PRIMARY KEY REFERENCES task(id), worktree TEXT NOT NULL, branch_key TEXT NOT NULL,
                config TEXT NOT NULL, stage TEXT NOT NULL, fork_id TEXT, child_id TEXT, detail TEXT NOT NULL,
                created TEXT NOT NULL, updated TEXT NOT NULL)""")
            self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS held_worktree ON unit(worktree) WHERE stage!='ready'""")
            self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS held_branch ON unit(branch_key) WHERE stage!='ready'""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS feedback (
                task_id TEXT NOT NULL, event_id TEXT NOT NULL, category TEXT NOT NULL,
                template_hash TEXT NOT NULL, PRIMARY KEY(task_id,event_id,category))""")
            self.db.execute("PRAGMA user_version=" + str(max(version, 2)))
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('namespace', ?)", (str(uuid.uuid4()),))
        self.client_factory = client_factory

    def close(self):
        self.db.close()

    @contextmanager
    def atomic(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def task(self, task_id):
        row = self.db.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        require(row is not None, "Unknown task ID")
        return dict(row)

    def update(self, task_id, **fields):
        fields["updated"] = now()
        self.db.execute("UPDATE task SET " + ",".join(key + "=?" for key in fields) + " WHERE id=? AND state NOT IN ('verified','accounted')",
                        (*fields.values(), task_id))

    def status(self, task_id):
        task = self.task(task_id)
        result = {key: (json.loads(task[key]) if task[key] and key == "verification" else task[key])
                  for key in ["id", "message_id", "registry_name", "worktree", "state", "cursor", "enqueued",
                              "delivered", "terminal", "hazard", "issue", "verification", "created", "updated"]}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='attempt'").fetchone():
            result["attempts"] = []
            for row in self.db.execute("SELECT * FROM attempt WHERE task_id=? ORDER BY number", (task_id,)):
                plan, previous = json.loads(row["plan"]), json.loads(row["previous"])["task"]
                result["attempts"].append({"number": row["number"], "state": row["state"],
                    "previous_message_id": previous["message_id"], "message_id": plan["message_id"],
                    "previous_model": json.loads(previous["registry"])["model"], "model": plan["model"],
                    "previous_terminal": previous["terminal"], "approval_hash": plan["approval_hash"],
                    "effects_checked": plan["effects_checked"]})
                if "baseline_approval" in plan:
                    result["attempts"][-1]["baseline_approval"] = plan["baseline_approval"]
        return result

    def approve(self, name, session_id, worktree, connection, *, _unit=None):
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name), "Invalid registry name")
        require(re.fullmatch(r"ses_[A-Za-z0-9_-]+", session_id), "Invalid session ID")
        client = self.client_factory(connection)
        local = identity(worktree)
        require(not self.path.resolve().is_relative_to(Path(local["worktree"])), "Keep the ledger outside the worktree")
        require(not local["status"], "Approval requires a clean worktree")
        binding = session_binding(client.json("GET", f"/api/session/{session_id}")["data"])
        require(binding["session_id"] == session_id and binding["directory"] == local["worktree"], "Session placement mismatch")
        require(client.idle(session_id), "Approval requires an idle session with no pending input/forms")
        snapshot = {**local, **binding, "connection": str(Path(connection).resolve()), "cursor": client.tail(session_id)}
        with self.atomic():
            self.check_lease(local, _unit, session_id)
            require(not self.db.execute("SELECT 1 FROM task WHERE (registry_name=? OR worktree=?) AND state NOT IN ('verified','accounted')",
                                        (name, local["worktree"])).fetchone(), "An unresolved assignment owns this session/worktree")
            for row in self.db.execute("SELECT name,snapshot FROM registry"):
                other = json.loads(row["snapshot"])
                require(row["name"] == name or other["session_id"] != session_id,
                        "This session is already registered under another name")
            self.db.execute("INSERT OR REPLACE INTO registry VALUES (?,?)", (name, encoded(snapshot)))
        return {"approved": name, "session_id": session_id, "worktree": local["worktree"], "base": local["head"]}

    def check_binding(self, registration, client):
        local = identity(registration["worktree"])
        for key in ["worktree", "common", "git_dir", "branch"]:
            if local[key] != registration[key]:
                raise DriftError("Worktree identity/branch drift")
        actual = session_binding(client.json("GET", f"/api/session/{registration['session_id']}")["data"])
        if not all(actual[key] == registration[key] for key in actual):
            raise DriftError("Session placement/model/agent drift")
        return local

    def check_lease(self, local, owner=None, session_id=None):
        row = self.db.execute("SELECT * FROM unit WHERE stage!='ready' AND (worktree=? OR branch_key=?)",
                              (local["worktree"], encoded([local["common"], local["branch"]]))).fetchone()
        require(not row or (owner == row["task_id"] and row["fork_id"] == session_id
                            and row["stage"] == "learning"), "A task/learning unit reserves this worktree/branch")
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='integration'").fetchone():
            row = self.db.execute("""SELECT task_id FROM integration WHERE stage NOT IN ('merged','deferred')
                AND (worktree=? OR branch_key=? OR (target=? AND stage IN ('approved','merging','uncertain')))""",
                (local["worktree"], encoded([local["common"], local["branch"]]), local["worktree"])).fetchone()
            require(not row or owner == row["task_id"], "An integration reserves this worktree/branch")

    def submit(self, task_id, name, text, paths, *, learning=None, _unit=None, _prompt=None, _template_hash=None):
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id), "Invalid task ID")
        require(text.strip() and len(text.encode()) <= 65536, "Task text must contain 1..65536 bytes")
        paths = allowed_paths(paths)
        request_hash = digest([name, text, paths, learning]) if learning is not None else digest([name, text, paths])
        existing = self.db.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        if existing:
            require(existing["request_hash"] == request_hash, "Task ID already names a different assignment")
            return self.status(task_id)  # Deliberately no second POST, even after a lost response.
        row = self.db.execute("SELECT snapshot FROM registry WHERE name=?", (name,)).fetchone()
        require(row is not None, "Session is not approved")
        registration = json.loads(row["snapshot"])
        require(not self.db.execute("""SELECT 1 FROM task WHERE state NOT IN ('verified','accounted')
                    AND (registry_name=? OR session_id=? OR worktree=? OR branch_key=?)""",
                    (name, registration["session_id"], registration["worktree"],
                     encoded([registration["common"], registration["branch"]]))).fetchone(),
                "An unresolved assignment owns this session/worktree/branch")
        client = self.client_factory(registration["connection"])
        local = self.check_binding(registration, client)
        require(local["head"] == registration["head"] and not local["status"], "Worktree HEAD/status drift; review and reapprove")
        require(client.idle(registration["session_id"]), "Session is busy or has pending input/forms")
        require(client.tail(registration["session_id"]) == registration["cursor"], "Session changed; review and reapprove")
        namespace = self.db.execute("SELECT value FROM meta WHERE key='namespace'").fetchone()[0]
        message_id = "msg_dispatch_" + uuid.uuid5(uuid.UUID(namespace), task_id).hex
        template = Path(__file__).with_name("templates").joinpath("task.txt").read_text()
        prompt = _prompt or Template(template).substitute(task_id=task_id, worktree=local["worktree"],
                    base=local["head"], paths=encoded(paths), assignment=text)
        payload = {"id": message_id, "text": prompt,
                   "metadata": {"dispatcher_task": task_id, "base_commit": local["head"],
                                "template_hash": _template_hash or digest(template), "prompt_hash": digest(prompt)},
                   "delivery": "queue", "resume": True}
        try:
            with self.atomic():
                self.check_lease(local, _unit, registration["session_id"])
                require(self.db.execute("SELECT snapshot FROM registry WHERE name=?", (name,)).fetchone()[0] == row["snapshot"],
                        "Registry changed during submission")
                self.db.execute("""INSERT INTO task
                  (id,message_id,session_id,registry_name,registry,worktree,branch_key,payload,request_hash,allowed,state,cursor,created,updated)
                  VALUES (?,?,?,?,?,?,?,?,?,?,'sending',?,?,?)""",
                  (task_id, message_id, registration["session_id"], name, row["snapshot"], local["worktree"], encoded([local["common"], local["branch"]]),
                   encoded(payload), request_hash, encoded(paths), registration["cursor"], now(), now()))
                if learning is not None:
                    from lifecycle import snapshot_policy
                    policy = snapshot_policy(learning, local["worktree"])
                    self.db.execute("INSERT INTO unit VALUES (?,?,?,?, 'task',NULL,NULL,?,?,?)",
                        (task_id, local["worktree"], encoded([local["common"], local["branch"]]),
                         encoded(policy), '{}', now(), now()))
        except sqlite3.IntegrityError:
            raise DispatchError("Duplicate task or unresolved session/worktree/branch assignment; nothing sent") from None
        try:
            local = self.check_binding(registration, client)
            require(local["head"] == registration["head"] and not local["status"], "Worktree changed before send")
            result = client.json("POST", f"/api/session/{registration['session_id']}/prompt", payload)["data"]
            require(result["id"] == message_id and result["sessionID"] == registration["session_id"]
                    and result["payload"]["text"] == prompt, "Admission response identity/payload mismatch")
            # time.created changes after delivery; it is not an identity check.
            self.db.execute("UPDATE task SET state='admitted',updated=? WHERE id=? AND state='sending'", (now(), task_id))
        except (DispatchError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            self.db.execute("UPDATE task SET state='uncertain',issue=?,updated=? WHERE id=? AND state='sending'",
                            ("Admission unconfirmed; reconcile without resending", now(), task_id))
        return self.status(task_id)

    def event(self, task_id, event):
        task = self.task(task_id)
        registration = json.loads(task["registry"])
        sid = registration["session_id"]
        require(event["durable"]["aggregateID"] == sid and event["data"]["sessionID"] == sid, "Wrong session in event log")
        seq, event_id, kind = event["durable"]["seq"], event["id"], event["type"]
        require(type(seq) is int and seq >= 0 and isinstance(event_id, str) and event_id.startswith("evt_")
                and isinstance(kind, str) and kind.startswith("session."), "Invalid durable event")
        with self.atomic():
            task = self.task(task_id)
            if task["state"] in ("verified", "accounted"):
                return  # A concurrent observer must not reopen a completed reservation.
            if seq <= task["cursor"]:
                old = self.db.execute("SELECT event_id,hash FROM observed WHERE task_id=? AND seq=?", (task_id, seq)).fetchone()
                require(old and old["event_id"] == event_id and old["hash"] == digest(event), "Conflicting replay")
                return
            require(seq == task["cursor"] + 1, "Session event gap; effects cannot be reconstructed")
            self.db.execute("INSERT INTO observed VALUES (?,?,?,?,?)", (task_id, seq, event_id, kind, digest(event)))
            fields = {"cursor": seq}
            data = event["data"]
            if kind == "session.inbox.enqueued":
                if data["inboxID"] == task["message_id"]:
                    payload = json.loads(task["payload"])
                    require(data["item"]["type"] == "user" and data["item"]["delivery"] == "queue"
                            and data["item"]["payload"]["text"] == payload["text"], "Stored admission differs from assignment")
                    fields["enqueued"] = 1
                elif data["item"]["type"] == "user":
                    fields["hazard"] = "Another user input overlaps this assignment"
            elif kind == "session.execution.started":
                if task["delivery_run"] is not None:
                    fields["hazard"] = "Execution restarted; repeated effects require review"
                fields["run_start"] = seq
            elif kind == "session.inbox.delivered" and data["inboxID"] == task["message_id"]:
                require(task["enqueued"] and task["run_start"] is not None, "Delivery has no attributable admission/execution")
                fields.update(delivered=1, delivery_run=task["run_start"])
            elif kind.startswith("session.execution.") and kind != "session.execution.started":
                if task["delivered"] and task["delivery_run"] == task["run_start"]:
                    fields["terminal"] = kind.rsplit(".", 1)[1]
                    if kind != "session.execution.succeeded":
                        fields["hazard"] = "Execution failed/interrupted; effects may already exist"
            elif kind in ("session.moved", "session.model.selected", "session.agent.selected"):
                fields["hazard"] = "Session placement/model/agent changed during assignment"
            # Native structured provider failures, not strings in tool output. A 429
            # does not establish exhaustion of the daily/free quota.
            if kind == "session.step.failed":
                error = data.get("error", {})
                category = ("provider_quota" if error.get("type") in (
                                "provider.free-limit", "provider.go-limit", "provider.quota", "provider.budget-limit",
                                "provider.model-budget-limit", "provider.chatgpt-limit")
                            else "provider_rate_limit" if error.get("type") == "provider.rate-limit"
                                or (str(error.get("type", "")).startswith("provider.") and error.get("status") == 429) else None)
                if category:
                    self.db.execute("INSERT OR IGNORE INTO feedback VALUES (?,?,?,?)",
                        (task_id, event_id, category, json.loads(task["payload"])["metadata"].get("template_hash", "legacy")))
            if kind in ("session.tool.failed", "session.step.failed"):
                error = encoded(data.get("error", {})).lower()
                categories = [code for code, words in (
                    ("command_guard", ("commandguard", "command guard", "commandguardrejected", "the /learn command", "the /review command")),
                    ("permission_denied", ("permission", "denied", "rejected")),
                    ("quota", ("quota", "rate limit", "rate_limit")),
                    ("tool_unavailable", ("unknown tool", "tool not found", "no tool named")),
                    ("edit_match_failed", ("could not find oldstring", "oldstring is not unique"))) if any(word in error for word in words)]
                for category in categories:
                    self.db.execute("INSERT OR IGNORE INTO feedback VALUES (?,?,?,?)",
                        (task_id, event_id, category, json.loads(task["payload"])["metadata"].get("template_hash", "legacy")))
            self.update(task_id, **fields)

    def reconcile(self, task_id):
        task = self.task(task_id)
        if task["state"] in ("verified", "accounted"):
            return self.status(task_id)
        registration = json.loads(task["registry"])
        try:
            client = self.client_factory(registration["connection"])
            try:
                self.check_binding(registration, client)
            except DriftError as error:
                self.update(task_id, state="uncertain", hazard=str(error))
                return self.status(task_id)
            marker = None
            for event in client.events(registration["session_id"], task["cursor"]):
                if event.get("type") == "log.synced":
                    require(event.get("aggregateID") == registration["session_id"], "Wrong session watermark")
                    marker = event.get("seq")
                else:
                    try:
                        self.event(task_id, event)
                    except (DispatchError, KeyError, TypeError, sqlite3.IntegrityError) as error:
                        self.update(task_id, state="uncertain", hazard="Unusable retained history: " + str(error))
                        return self.status(task_id)
            task = self.task(task_id)
            if marker != task["cursor"]:
                self.update(task_id, state="uncertain", hazard="Missing replay watermark/events; effects unknown")
                return self.status(task_id)
            idle = client.idle(registration["session_id"])
            if task["hazard"]:
                self.update(task_id, state="uncertain", issue=None)
            elif task["delivered"] and task["terminal"] == "succeeded" and idle:
                self.update(task_id, state="completed", issue=None)
            elif task["delivered"] and not idle:
                self.update(task_id, state="running", issue=None)
            elif task["enqueued"] and not task["delivered"]:
                self.update(task_id, state="admitted", issue=None)
            else:
                self.update(task_id, state="uncertain", issue="No attributable terminal completion; inspect actual Git/effects")
        except (DispatchError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            self.update(task_id, state="uncertain", issue="Observation incomplete; cursor retained, no request retried")
        return self.status(task_id)

    def verify(self, task_id, commit):
        self.reconcile(task_id)
        task = self.task(task_id)
        registration = json.loads(task["registry"])
        require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit), "Supply a full commit hash")
        local = identity(registration["worktree"])
        require(all(local[key] == registration[key] for key in ("worktree", "common", "git_dir", "branch")), "Worktree identity drift")
        require(not local["status"] and local["head"] == commit, "Verify requires a clean worktree at the supplied commit")
        recovered = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='attempt'").fetchone() and self.db.execute(
            "SELECT 1 FROM attempt WHERE task_id=? AND state='observing'", (task_id,)).fetchone()
        if recovered:
            commits, _ = task_history(local["worktree"], registration["head"], commit, json.loads(task["allowed"]), task_id)
            paths = git(local["worktree"], "diff", "--name-only", "--no-renames", "-z", registration["head"], commit).decode().split("\0")[:-1]
            require(commits and paths, "Recovered task has no actual committed delivery")
        else:
            parents = git(local["worktree"], "show", "-s", "--format=%P", commit).decode().split()
            require(parents == [registration["head"]], "Expected one normal commit directly after the approved base")
            paths = git(local["worktree"], "diff", "--name-only", "--no-renames", "-z", registration["head"], commit).decode().split("\0")[:-1]
            require(paths and set(paths) <= set(json.loads(task["allowed"])), "Commit changed paths outside the assignment")
            message = git(local["worktree"], "show", "-s", "--format=%B", commit).decode()
            require(f"OpenCode-Task: {task_id}" in message.splitlines(), "Commit lacks the exact task trailer")
            commits = [commit]
        proof = {"kind": "git_scope", "base": registration["head"], "commit": commit,
                 "tree": git(local["worktree"], "rev-parse", commit + "^{tree}").decode().strip(),
                 "paths": paths, "commits": commits, "verified_at": now(), "human_review_required": True,
                 "non_git_effects": "not established by Git verification"}
        state = "verified" if task["state"] in ("completed", "verified") else "uncertain"
        self.update(task_id, state=state, verification=encoded(proof))
        return self.status(task_id)

    def result(self, task_id, patch=False):
        task = self.task(task_id)
        require(task["verification"], "No verified Git result; run verify with an explicit commit")
        proof = json.loads(task["verification"])
        registration = json.loads(task["registry"])
        local = identity(registration["worktree"])
        require(not local["status"] and local["head"] == proof["commit"] and local["branch"] == registration["branch"]
                and local["common"] == registration["common"] and local["git_dir"] == registration["git_dir"],
                "Worktree drift since verification; inspect before integration")
        if patch:
            return {**self.status(task_id), "patch": git(local["worktree"], "show", "--format=fuller",
                    "--no-ext-diff", "--no-textconv", proof["commit"]).decode()}
        return self.status(task_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Private ledger outside worktrees")
    commands = parser.add_subparsers(dest="command", required=True)
    approve = commands.add_parser("approve", help="Explicitly approve an idle existing root session")
    approve.add_argument("name")
    approve.add_argument("--connection", required=True)
    approve.add_argument("--session", required=True)
    approve.add_argument("--worktree", required=True)
    submit = commands.add_parser("submit", help="Persist and send one assignment; never resend an existing task")
    submit.add_argument("task_id")
    submit.add_argument("--learning-policy", help="Opt in future task to a snapshotted post-task learn policy JSON")
    submit.add_argument("--session", required=True, help="Approved registry name")
    submit.add_argument("--task-file", required=True)
    submit.add_argument("--allow", action="append", required=True, help="Exact repository-relative file; repeat as needed")
    for command in ("reconcile", "status", "verify", "result"):
        sub = commands.add_parser(command)
        sub.add_argument("task_id")
        if command == "verify":
            sub.add_argument("--commit", required=True)
        if command == "result":
            sub.add_argument("--patch", action="store_true")
    args = parser.parse_args(argv)
    dispatcher = None
    try:
        dispatcher = Dispatcher(args.db)
        if args.command == "approve":
            result = dispatcher.approve(args.name, args.session, args.worktree, args.connection)
        elif args.command == "submit":
            result = dispatcher.submit(args.task_id, args.session, Path(args.task_file).read_text(), args.allow,
                learning=json.loads(Path(args.learning_policy).read_text()) if args.learning_policy else None)
        elif args.command == "verify":
            result = dispatcher.verify(args.task_id, args.commit)
        elif args.command == "result":
            result = dispatcher.result(args.task_id, args.patch)
        else:
            result = getattr(dispatcher, args.command)(args.task_id)
        print(result if isinstance(result, str) else json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        if dispatcher and getattr(args, "task_id", None):
            row = dispatcher.db.execute("SELECT state FROM task WHERE id=?", (args.task_id,)).fetchone()
            if row and row["state"] not in ("verified", "accounted"):
                dispatcher.update(args.task_id, state="uncertain", issue="Operator interrupted observation/send; no retry performed")
        print("dispatcher: interrupted; reconcile saved state before further action", file=sys.stderr)
        return 130
    except (DispatchError, OSError, ValueError, KeyError, TypeError, sqlite3.Error, http.client.HTTPException) as error:
        print(f"dispatcher: {error}", file=sys.stderr)
        return 1
    finally:
        if dispatcher:
            dispatcher.close()


if __name__ == "__main__":
    sys.modules["dispatch"] = sys.modules[__name__]
    raise SystemExit(main())
