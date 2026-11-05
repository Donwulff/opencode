#!/usr/bin/env python3
"""One explicit, nonrecursive Codex review of a frozen integration unit. Never merges."""
import argparse
import json
import os
from pathlib import Path
import shutil
from string import Template
import subprocess
import tempfile

from dispatch import Dispatcher, DispatchError, digest, encoded, git, require
from lifecycle import Lifecycle


RISKS = {
    "source_ambiguity": "xhigh", "memory_safety": "xhigh", "conflicting_evidence": "xhigh",
    "failed_tests": "high", "unexpected_diff": "high", "api_abi_change": "high",
    "operator_approval": "high",
}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["assessment", "issues", "escalation"],
          "properties": {"assessment": {"type": "string", "enum": ["routine", "needs_escalation", "blocked"]},
                         "issues": {"type": "array", "items": {"type": "string"}},
                         "escalation": {"type": "array", "items": {"type": "string", "enum": list(RISKS)}}}}


def plan(dispatcher, task_id, model, risks, evidence, *, integration=None):
    require(model and all(risk in RISKS for risk in risks), "Explicit model and known risk flags required")
    unit = Lifecycle(dispatcher).result(task_id)
    # Read acknowledgements are revalidated, but never change the review's immutable input.
    unit.pop("post_verification_views", None)
    proof = unit["task"]["verification"]
    commits = [("task", proof["base"], proof["commit"])]
    learning_commit = unit["learning"]["detail"].get("learning_commit")
    if learning_commit:
        commits.append(("learning", proof["commit"], learning_commit))
    patches = [{"phase": phase, "base": base, "commit": commit,
                "diff": git(unit["task"]["worktree"], "diff", "--no-ext-diff", "--no-textconv", "--no-renames", base, commit).decode()}
               for phase, base, commit in commits]
    native = (".c", ".h", ".xs", ".cc", ".cpp", ".patch", ".diff")
    source_paths = [path for path in proof["paths"] if path.endswith(native)]
    risks = sorted(set(risks))
    if unit["learning"]["detail"].get("learning_commit") and unit["learning"]["detail"]["learning"] != "completed":
        risks = sorted(set(risks) | {"unexpected_diff"})
    effort = "xhigh" if any(RISKS[risk] == "xhigh" for risk in risks) else "high" if risks else "medium"
    template = Path(__file__).with_name("templates").joinpath("review.txt").read_text()
    assignment = encoded({"unit": unit, "risk_flags": risks, "source_paths": source_paths,
                          "operator_evidence": evidence, "patches": patches, "integration": integration})
    require(len(assignment.encode()) <= 1024 * 1024, "Review input too large; narrow the review rather than truncate evidence")
    prompt = Template(template).substitute(assignment=assignment)
    return {"model": model, "effort": effort, "risk_flags": risks, "template_hash": digest(template),
            "unit_hash": digest(unit), "evidence_hash": digest(evidence), "integration_hash": digest(integration),
            "input_hash": digest(prompt), "prompt": prompt, "human_integration_approval_required": True}


def run(dispatcher, task_id, review, timeout=600, runner=subprocess.run, escalate=False):
    # The historical column name is task_id. An integration gate uses its stable
    # integration ID here, so a deliberately replanned target has a separate review
    # scope while retaining both results. Each scope still permits at most one escalation.
    require(not os.environ.get("CODEX_THREAD_ID") and not os.environ.get("CODEX_SESSION_ID"),
            "Refusing nested Codex execution; run from an ordinary operator shell")
    executable = shutil.which("codex")
    require(executable, "Codex must already be installed")
    require(1 <= timeout <= 1800, "Review timeout must be 1..1800 seconds")
    dispatcher.db.execute("""CREATE TABLE IF NOT EXISTS review (
        task_id TEXT NOT NULL, attempt INTEGER NOT NULL, input_hash TEXT NOT NULL,
        state TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(task_id,attempt))""")
    # Explicit retries/escalations use plan/feedback; never resubmit a paid run automatically.
    with dispatcher.atomic():
        prior = dispatcher.db.execute("SELECT * FROM review WHERE task_id=? ORDER BY attempt", (task_id,)).fetchall()
        if prior:
            require(escalate and len(prior) == 1 and prior[0]["state"] not in ("running", "uncertain"),
                    "A review attempt already exists; only one explicit confirmed higher-effort escalation is permitted")
            levels = {"medium": 0, "high": 1, "xhigh": 2}
            previous = json.loads(prior[0]["result"])["plan"]
            require(levels[review["effort"]] > levels[previous["effort"]], "Escalation must increase reasoning effort")
        else:
            require(not escalate, "No review exists to escalate")
        attempt = len(prior) + 1
        dispatcher.db.execute("INSERT INTO review VALUES (?,?,?,'running',?)", (task_id, attempt, review["input_hash"], encoded({"plan": review})))
    with tempfile.TemporaryDirectory(prefix="dispatch-review-") as directory:
        root = Path(directory)
        schema, output = root / "schema.json", root / "result.json"
        schema.write_text(encoded(SCHEMA))
        command = [executable, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                   "--sandbox", "read-only", "--model", review["model"],
                   "-c", 'model_reasoning_effort="' + review["effort"] + '"',
                   "--disable", "multi_agent", "--disable", "multi_agent_v2",
                   "--disable", "shell_tool", "--disable", "unified_exec",
                   "--disable", "apps", "--disable", "plugins", "--disable", "hooks",
                   "--disable", "unbounded_connection_retries", "-c", 'web_search="disabled"',
                   "--output-schema", str(schema), "--output-last-message", str(output), "-"]
        try:
            completed = runner(command, input=review["prompt"], text=True, cwd=root, capture_output=True, timeout=timeout)
            require(completed.returncode == 0 and output.exists(), "Review did not return a successful structured result")
            require(output.stat().st_size <= 1024 * 1024, "Structured review exceeds read limit")
            result = json.loads(output.read_text())
            require(isinstance(result, dict) and set(result) == {"assessment", "issues", "escalation"}
                    and result["assessment"] in ("routine", "needs_escalation", "blocked")
                    and isinstance(result["issues"], list) and all(isinstance(item, str) for item in result["issues"])
                    and isinstance(result["escalation"], list) and all(isinstance(item, str) and item in RISKS for item in result["escalation"]),
                    "Invalid structured review result")
            if result["assessment"] == "blocked":
                state = "blocked"
            elif result["issues"] or result["escalation"] or result["assessment"] == "needs_escalation":
                state = "needs_escalation"
            else:
                state = "reviewed_with_risks" if review["risk_flags"] else "reviewed"
            dispatcher.db.execute("UPDATE review SET state=?,result=? WHERE task_id=? AND attempt=?",
                                  (state, encoded({"plan": review, "assessment": result}), task_id, attempt))
            return {"state": state, "result": result, "human_integration_approval_required": True}
        except (DispatchError, OSError, ValueError, subprocess.TimeoutExpired):
            dispatcher.db.execute("UPDATE review SET state='uncertain',result=? WHERE task_id=? AND attempt=?",
                                  (encoded({"plan": review, "issue": "No confirmed review result; no automatic retry"}), task_id, attempt))
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("task_id")
    parser.add_argument("--model")
    parser.add_argument("--risk", choices=sorted(RISKS), action="append", default=[])
    parser.add_argument("--evidence", help="Reviewed test/evidence summary file; not executable")
    parser.add_argument("--run", action="store_true", help="One paid review, only from an ordinary shell")
    parser.add_argument("--escalate", action="store_true", help="Explicitly permit one higher-effort follow-up to a confirmed review")
    parser.add_argument("--result", action="store_true", help="Read the saved review attempt without starting inference")
    args = parser.parse_args()
    dispatcher = None
    try:
        dispatcher = Dispatcher(args.db)
        if args.result:
            require(dispatcher.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review'").fetchone(), "No review attempts")
            rows = dispatcher.db.execute("SELECT * FROM review WHERE task_id=? ORDER BY attempt", (args.task_id,)).fetchall()
            require(rows, "No saved review for this task")
            print(json.dumps([{**dict(row), "result": json.loads(row["result"])} for row in rows], indent=2))
            return 0
        require(args.model and args.evidence, "--model and --evidence are required to plan/run a review")
        review = plan(dispatcher, args.task_id, args.model, args.risk, Path(args.evidence).read_text())
        print(json.dumps(run(dispatcher, args.task_id, review, escalate=args.escalate) if args.run else review, indent=2))
        return 0
    except (DispatchError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"review: {error}", file=__import__("sys").stderr)
        return 1
    finally:
        if dispatcher:
            dispatcher.close()


if __name__ == "__main__":
    raise SystemExit(main())
