# Reserved task → learning → integration unit

Opt-in extension of [the existing dispatcher](README.md), in the **same private
ledger**. The learning lifecycle selects no tasks, merges no branches, changes
no original session's model/permissions, and performs no investigation or package
installation.

## Use for future assignments

Use an approved idle persistent session on a clean worktree and a service with
retained events, as described in README. Existing live tasks are not adopted.
Prepare a private policy JSON outside the repository, for example:

```json
{
  "prompt": "/absolute/worktree/.opencode/command/learn.md",
  "model": {"providerID": "vLLM", "id": "Qwen/Qwen3.8-27B-FP8"},
  "allow": ["AGENTS.md", "findings/Pool/AGENTS.md"],
  "max_active": 3,
  "wait_seconds": 21600
}
```

Use actual existing **tracked** instruction paths, not the example Pool path if
it does not exist. The model must already be configured on that service. These
provider/model IDs match the project's current Qwen configuration; they are not
hard-coded defaults. No model is downloaded or installed. The prompt is read
literally: frontmatter and `$ARGUMENTS` are removed, with no shell, file-reference
or custom-command expansion. Its body and the learning wrapper are snapshotted
at submission, including hashes. Frontmatter agent choices are not applied;
the fork inherits the task's agent and gets the explicit learning model.

```sh
python3 dispatch.py --db "$DISPATCH_STATE/ledger.sqlite3" submit docs-002 \
  --session docs --task-file "$DISPATCH_STATE/task.txt" --allow README.md \
  --learning-policy "$DISPATCH_STATE/learning-policy.json"

python3 lifecycle.py --db "$DISPATCH_STATE/ledger.sqlite3" watch docs-002 \
  --gpu-available --seconds 10800 --interval 5

python3 lifecycle.py --db "$DISPATCH_STATE/ledger.sqlite3" status docs-002
python3 lifecycle.py --db "$DISPATCH_STATE/ledger.sqlite3" result docs-002
python3 lifecycle.py --db "$DISPATCH_STATE/ledger.sqlite3" feedback
```

`watch` accepts several **explicitly enrolled** task IDs, not a discover-and-fill
queue. `advance TASK --gpu-available` performs one cycle. Without the GPU flag,
task verification continues but no learning starts. The flag authorizes GPU
admission for that bounded invocation; choose `--seconds` to end before other
GPU use. Stop/restart the watcher without the flag to close admission: stopping
the watcher does not interrupt model execution. Nothing cancels running work.
Watch exits 0 when all units are ready, 1 for a review hold/error, or 2 when its
time bound ends with unfinished units. Status output distinguishes those states.

Admission counts active sessions using the learning provider and this ledger's
fork reservations, including queued and uncertain admissions. Maximum is 1–4;
the default remains three. A fourth slot requires explicit resource authorization
and a matching provider-pool capacity. Existing policy snapshots stay unchanged.
This is a conservative cooperative limit, not KV-memory measurement. It cannot
see other inference clients, provider aliases on the same GPU, queued work from
other dispatchers or other OpenCode hosts. Use one host, one ledger and the
operator availability gate; account for outside GPU consumers before enabling
it. The general resource scheduler in RESOURCE_CONTROL remains a design.

## Deterministic accounting

| Unit stage | Meaning |
| --- | --- |
| `task` | Assignment reserved before its single prompt POST; native events and actual Git still required. |
| `await_gpu` | Original task commit passed the existing Git verifier; worktree remains held. |
| `forking` / `configuring` | Once-only mutation intent persisted. Interrupted operation requires inspection, not another fork. |
| `learning` | Stable child task/message ID, queued ordinary prompt on the disposable fork. |
| `accounting` | Scoped local commit intent; interrupted Git hooks/commit are never rerun automatically. |
| `ready` | Learning succeeded, failed, made no changes, or was skipped before admission; all known output accounted. |
| `needs_review` | Drift, mixed input, missing evidence or uncertain effects; reservation remains held. |

Task verification retains the original one-parent commit, exact task trailer,
allowed-path and clean-worktree checks. Native successful completion alone is
insufficient. Verification does not release an enrolled unit's worktree. The
fork inherits projected conversation and current instructions; **it shares the
filesystem**. Original continuation, changed branch/model, dirty index, unexpected
files, extra commits or changed history stop the lifecycle. After verification,
only contiguous durable `session.viewed` acknowledgements may follow the frozen
cursor. They are reported separately and do not change the task proof or review
input. Any other event or missing replay/watermark still stops it. Cooperating
watchers serialize lifecycle mutations through a ledger-file lock. This does not lock
humans or independent filesystem writers out of the checkout.

Learning runs the existing prompt automatically, including its own decision
whether any lesson is useful; there is no preliminary LLM or human lesson judge.
The fork has deny-by-default session permissions, read access and exact-path edit
access to the approved AGENTS files. Shell, delegation and network tools are
unavailable. Source instructions/reference files remain read-only. No command
marker or `/command` call is used, avoiding the slash-command guard lifecycle.

On attributable terminal completion **and** idle/no-pending-input state:

- No diff: record `completed_no_changes`; no empty commit.
- Scoped diff: coordinator makes a separate ordinary learning-proposal commit,
  with child `OpenCode-Task`, parent `OpenCode-Learn` and `Learning-Outcome`
  trailers. It does not amend the task commit. Local Git identity and hooks apply.
- Failure/interruption: preserve the verified correction. Record the failure;
  preserve any scoped partial proposal as a separately labelled commit. No
  claim that it is a successful task, and no retry. A known failed terminal with
  clean idle state can be accounted; missing terminal/outcomes cannot.
- Quota/tool failures: retain native event references in feedback. A quota error
  recorded in a tool/step event is not itself proof that execution stopped;
  terminal attribution and idle checks still apply.
- No capacity within the configured wait: skip **before fork admission**, record
  `skipped_gpu_unavailable` or `skipped_gpu_capacity`, and release the unit.

The child task becomes `accounted`, distinct from the original `verified` task.
The fork is archived, retaining its history. Unconfirmed archival is reported
without discarding a completed result. A ready receipt presents both commits,
learning outcome, prompt snapshots and feedback as **one integration unit**;
human review/integration stays separate. Use `lifecycle.py result`, since basic
`dispatch.py result` deliberately expects the task commit still to be HEAD.

No learning child is enrolled for another learning run. Repeated `advance` after
`ready` does nothing. The original task and evidence stay authoritative.
Uncertain fork creation has no native idempotency key: do not replace it with a
new task ID, edit the ledger to evade its hold, or repeat the POST. Manual Git
and session inspection remains necessary for `needs_review`; no unsafe recovery
override is supplied. The original task proof remains available via `status`.

## Medium-effort review, explicit escalation

From an **ordinary operator shell**, plan a frozen structured review:

```sh
python3 review.py --db "$DISPATCH_STATE/ledger.sqlite3" docs-002 \
  --model gpt-6.1-sol --evidence "$DISPATCH_STATE/validation-summary.txt"
# Add --run for one paid noninteractive Codex review.
python3 review.py --db "$DISPATCH_STATE/ledger.sqlite3" docs-002 --result
```

No global model/effort settings change. Routine review uses **medium**, even if
the operator's normal Codex effort is xhigh. Exact Git checks run first at every
effort. Supply the real test/source/DSO evidence and limitations; the tool does
not turn an operator summary into verified test success or run tests itself.

| Criterion (`--risk`) | Effort / action |
| --- | --- |
| `failed_tests`, `unexpected_diff`, `api_abi_change` | high; failed checks/contracts still require resolution. |
| `source_ambiguity`, `memory_safety`, `conflicting_evidence` | xhigh; source/evidence judgment is necessary. |
| `operator_approval` | high advisory review; only the operator can authorize the action. |

Native C/XS/header and patch paths are identified in the review input; file type
alone does not establish ambiguity or force xhigh. A failed learning proposal
with edits selects `unexpected_diff`. Otherwise the medium reviewer must return
named escalation criteria when correctness exceeds routine evidence checking.
For one **confirmed** review, an operator may run again with `--run --escalate`
and a higher-effort risk flag/evidence. Maximum two attempts per unit; both
snapshots/results survive. Unknown/running/timeout outcomes cannot be retried
or escalated automatically. This is advisory review, never merge authorization.

The runner refuses execution inside `CODEX_SESSION_ID`/`CODEX_THREAD_ID`. It
uses `codex exec`, explicit model/effort, a strict output schema, an ephemeral
read-only temporary directory, no inherited user config, disabled multi-agent,
shell, apps/plugins/hooks, disabled web search/unbounded connection retries and
a 600-second timeout. It reuses existing CLI authentication. No nested Codex
session or direct inference client is created by the OpenCode watcher. Model
calls consume quota only when the operator explicitly uses `--run` outside
Codex; provider-internal bounded retries are not an exactly-once billing claim.

## Approved integration gate

This optional extension uses the same ledger, task receipts, learning lifecycle
and review runner. Enroll a **future eligible task** after submission (before its
worktree is reassigned). The target must be a separate clean worktree/branch in
the same repository, with no active or pending registered assignment. No live
session is adopted, switched or interrupted. Enrolling opts this ledger into
schema 5; ordinary dispatch/recovery preserve that version. Existing completed
units whose worktree has since advanced cannot be retroactively enrolled.

```sh
python3 integrate.py --db "$DISPATCH_STATE/ledger.sqlite3" enroll docs-002 \
  --target /absolute/path/to/integration-worktree --model gpt-6.1-sol \
  --evidence "$DISPATCH_STATE/validation-summary.txt"
# Save the returned integration ID as UNIT. Learning still uses its existing policy.
python3 integrate.py --db "$DISPATCH_STATE/ledger.sqlite3" advance "$UNIT"
python3 integrate.py --db "$DISPATCH_STATE/ledger.sqlite3" status "$UNIT"
# From an ordinary operator shell; one explicit paid review, never nested in Codex:
python3 integrate.py --db "$DISPATCH_STATE/ledger.sqlite3" watch "$UNIT" \
  --run-review --seconds 3600
# After reviewing the unit and confirmed structured assessment:
python3 integrate.py --db "$DISPATCH_STATE/ledger.sqlite3" approve "$UNIT"
python3 integrate.py --db "$DISPATCH_STATE/ledger.sqlite3" advance "$UNIT"
```

Without `--gpu-available`, no learning is admitted; its existing bounded wait/skip
policy applies. Without `--run-review`, no model review starts. Watch accepts
explicit IDs and stops at approval, substantive review holds, conflicts, uncertain
effects, or its time bound. Its bound stops further cycles, not a review/merge
already admitted (those have their own timeouts). No automatic task selection,
continuation, integration approval, branch synchronization or container management
is introduced. Use the existing queue for separately approved future assignments.

`waiting_task` advances the existing lifecycle; `await_review` freezes source and
target identities/HEADs, the full unit, evidence and merge-tree preflight. Review
includes target changes since the task base and the proposed target diff, alongside
separate task/learning diffs. Reviews are keyed by the stable integration ID;
`review.py --db LEDGER "$UNIT" --result` retrieves the saved attempts. A deliberate
deferral/re-enrollment after target advancement retains the old review and requires
a new target-specific review; no observer silently retargets or restarts review.
`await_approval` requires a confirmed routine result
with zero issues/escalations. Review effort remains medium unless known risks
require high/xhigh; all deterministic checks remain mandatory. To request the
existing one explicit higher-effort escalation, use `review UNIT --escalate
--risk CRITERION`. It reviews the same frozen evidence; changed evidence needs a
new plan and explicit review, never a silent retry. Substantive issues cannot be
waived with `approve`. A successful higher-effort review requires every risk to
be acknowledged using repeated `approve --accept-risk CRITERION` options.

Approval binds the exact snapshot and confirmed review attempt. `approved`
rechecks identity, HEAD, status, session activity, unit and review before Git.
`merged` records actual parents, tree and source reachability; original task and
learning commits remain separate. A conflict is detected by `git merge-tree`
before any target index/ref/worktree change (Git objects may be created). Custom
merge drivers require manual review; actual merge hooks remain enabled.

Source reservations last until `merged` or explicit `defer UNIT --reason TEXT`.
The target is additionally held once approved. `merging` is persisted **before**
Git/hooks; an interrupted, failed or unobserved merge becomes `uncertain`, even
if a commit actually exists. No retry, reset, abort or reservation override is
provided: inspect actual Git/hook effects manually. An explicit `defer` can revoke
approval only before `merging` intent, retaining the approval record; it cannot
release an uncertain merge. Source/target drift invalidates the plan and stops
it; resolve it explicitly rather than editing the ledger to bypass checks.
Cooperative leases do not fence humans, other hosts/ledgers or filesystem writers.
Test summaries remain supplied evidence, not independently rerun test results.

Fixture tests cover real history-preserving merges, separate learning, duplicate
enrollment, reconnect, target/source drift, preflight conflicts, explicit risk
approval, target/session collisions and lost merge outcomes before/after commit.
Paid Codex review remains unrun from a Codex session; the existing nested guard
is intentional. Live sessions/ledgers are not test fixtures or automatically
migrated. Run `python3 -m unittest test_dispatch test_lifecycle test_batch
test_recovery test_integrate` from this directory. The extension checkpoint passed
117 distinct fixture tests (full suite, then the final integration rerun) and
the native learning smoke: one queued task, eight fake-provider steps, retained
cursor 31 and a separate learning commit. No paid review was performed.

## Prompt feedback and remaining guard boundary

`templates/task.txt`, `learn.txt` and `review.txt` contain current prompts;
logic/state transitions remain in Python. Edit templates through normal review.
Already submitted payloads and learning snapshots never change. Native failure
events produce category counts, task/event references and template hashes only,
not raw error bodies. These are keyword-based feedback signals, not causal
security classifications. Use recurring groups to review a template change;
agent output never rewrites templates automatically.

**Dedicated-edit enforcement is incomplete at the API boundary.** The native
`edit`, `write` and `patch` tools all use permission action `edit`. Exact-file
permissions therefore cannot distinguish them. The prompt requires `edit`; a
successful different writer is detected from durable tool events and labelled
`failed_tool_policy`, with its scoped proposal preserved for review. The native
slash-command guard also groups file writers; it is not an edit-only sandbox.
No core/plugin change or permission weakening is made to solve this. Git checks
cover tracked and ordinary untracked files, not every ignored artifact, external
filesystem effect or concurrent writer. Keep forks disposable and isolated from
other assignments as required by the reservation protocol.

## Verified capability and coverage

Checked against **Codex CLI 0.166.0**, **Bun 1.4.4**, and OpenCode checkout
`8604ecc70ec314f444410d3646be1c9ca5552b8e` (package version 2.2.5).
The live managed server reports `0.0.0-dev-202611011851`; it was queried only for
version/capabilities, never changed. Coordination records previously reported
missing retained events on that host. This prototype does **not** claim that
its live sessions have become eligible: `approve` still requires retained replay.
Use the dedicated persistence-enabled host; moving active sessions is outside
this change. No `opencode` executable is on this execution environment's PATH.

Local CLI help/features confirm model/effort overrides, `review`, `exec`, JSON
Schema results and enabled native multi-agent support. Model-specific subagents
and effort overrides are also documented in [OpenAI's subagent guide](https://learn.chatgpt.com/docs/agent-configuration/subagents).
They inherit parent settings unless overridden; they are not necessary for this
bounded runner. [Noninteractive structured results](https://learn.chatgpt.com/docs/non-interactive-mode)
use `exec --output-schema`; the installed standalone `review` help has no schema
option. No global agent definitions or nested review sessions were installed.

Native API sources: [session endpoints](../../packages/protocol/src/groups/session.ts),
[fork implementation](../../packages/core/src/session.ts),
[fork projection](../../packages/core/src/session/projector.ts),
[permission evaluation](../../packages/core/src/permission.ts),
[command guard](../../packages/core/src/tool/command-guard.ts), and
[writer permission aliases](../../packages/core/src/tool/plugin/write.ts).
`POST /api/session/:id/fork {}` copies through the latest projected message;
`PATCH /api/session/:fork` and `POST /api/session/:fork/model` configure only
that fork. Learning uses existing stable-ID `POST .../prompt`, queue/resume and
durable replay; it does not use `POST .../command`. API command discovery gives
descriptions, not resolved prompt bodies, hence the explicit local prompt path.

Tests use disposable Git/API fixtures for duplicate assignments, reservation
retention, lost fork responses, observer reconnect, source/worktree drift,
quota/failure/no-op accounting, non-edit writers, migration and bounded review
escalation. `native_smoke.py --learning` additionally uses the actual source API
and native tools with a loopback fake provider: preserved context, same checkout,
fork-only model override, shell filtering, queued delivery and a separate real
learning commit. No real Qwen/quota call is used. Codex execution is mocked:
CLI capabilities are inspected, but a paid end-to-end review is intentionally
unrun from this Codex session. Run that one explicit pilot from an ordinary shell.

Checkpoint validation: 46 stdlib fixture tests passed. The native learning smoke
passed with one original queued admission, four task-provider steps plus four
learning-provider steps, retained task replay through sequence 31, a real
separate learning commit, and no active-session or real-model use.
