# Plan-driven unattended operation — design and implementation plan

Design note (2026-11-08). This document defines how the existing dispatcher
evolves from manually assembled batches into unattended plan-driven operation:
the operator authorizes a plan, starts one controller, and returns to
review-ready results or named blockers. It is a planning document; no
functional changes are made by this commit.

Target operator experience:

```
authorize plan (commit plan artifact)  ->  controller start  ->  unattended
execution  ->  consolidated report: verified units + precise blockers
```

The design extends [the existing queue](README.md#bounded-unattended-queue),
[lifecycle](LIFECYCLE.md), [recovery](README.md#checkpointed-task-continuation)
and [integration gate](LIFECYCLE.md#approved-integration-gate) in the same
Python/stdlib/SQLite architecture. No new framework, service, database, or
external dependency is introduced.

**Implementation review, 2026-11-09.** The queue already admits an arbitrarily
deep approved backlog, observes recovery, verifies Git and accounts for learning;
another three-task batch is a coordination choice, not a queue limit. The live
schema-5 ledger had no dependency fields/edges: `depends_on` below was design
only. The bounded schema-6 prototype now adds task-ID prerequisites (verified
delivery plus accounted learning, actual integration ancestry, or explicit
result approval), admission proofs and assignment-scoped holds; see
[the implemented contract](README.md#task-id-prerequisites). It is fixture-tested,
not activated on the running observer. Recovery and integration APIs exist but
need explicit approvals; all 20 observed integration enrollments were deferred
in favor of manual merges. Automatic reapproval, plan import/selection, reusable
worktree allocation and follow-up drafting remain proposals.

Do not implement the proposed path-intersection `done` shortcut below: sharing
an output path is not proof of completing an acceptance criterion. Nor does an
agent's plan commit establish operator authorization. Dependencies gate approved
assignments; they do not supply missing source inputs or approve future bases.
The global-hold stop condition in §3.5 is superseded by scoped holds; conservative
resource reservations, explicit recovery and human integration approval remain.
Any future approved-task importer should store operational state and dependencies
in SQLite, not create a second Git-maintained task-status board beside PLAN.

## 1. Current-state architecture

### 1.1 Components and responsibilities

| Component | File | Responsibility |
| --- | --- | --- |
| Native API client | `dispatch.py` `Client` (54-156) | Loopback-only HTTP; SSE replay with `log.synced` watermark; idle check (active + inbox + form); `viewed_suffix` post-verification acknowledgement check. |
| Core ledger + assignment | `dispatch.py` `Dispatcher` (214-569) | Private SQLite ledger (application ID `OCDP`, user_version 5); `approve` pins session/project/model/agent, directory, Git common dir/branch/HEAD, event cursor; `submit` persists the exact request before its single `POST /api/session/:id/prompt` (`delivery:"queue", resume:true`) and never resends; `reconcile` replays durable events into `observed` and derives `sending/admitted/running/completed/uncertain`; `verify` requires clean worktree at the supplied commit, exactly one parent equal to the approved base, nonempty diff within allowed paths, and the exact `OpenCode-Task:` trailer. |
| Bounded queue | `batch.py` (24-267) | `pool` rows (connection/provider binding, capacity, external, state, `until_at`); `queued` rows (`pending/active/ready/blocked/deferred/continuing`); `enqueue` freezes the assignment spec (registration, learning policy snapshot, template hash, both pools); `tick` observes active units, admits pending units as capacity permits, and reports phase `running/draining/paused/blocked/complete`. Ledger-file lock serializes writers. |
| Capacity accounting | `batch.py` `availability`/`inference_held` (106-165) | Busy = native active sessions on the provider + ledger-reserved sessions + learning-unit reservations. Inference reservation for a failed/interrupted task ends only for an explicitly `deferred` queued row with a fully proven settled stop; everything else stays conservatively held. |
| Learning lifecycle | `lifecycle.py` (46-241) | Unit stages `task → await_gpu → forking → configuring → learning → accounting → ready | needs_review`; once-only native fork, deny-by-default permissions plus exact-path edit on allowed `AGENTS.md` files, stable child task ID `learn_<digest32>`, coordinator-made scoped learning commit with `OpenCode-Learn` trailer, fork archival. Bounded pre-admission GPU wait/skip. |
| Checkpointed continuation | `recovery.py` (25-324) | `plan` snapshots checkpoint (HEAD, partial file hashes, index hash, commits, per-tool outcome evidence) and returns an `approval_hash`; `prepare` (with explicit approval and optional `--effects-checked`, optional `--accept-base`) persists one continuation intent; `advance_locked` performs a model handoff with event-watermark checks and one prompt POST. Never replays a prompt. |
| Review | `review.py` (16-147) | One explicit, nonrecursive Codex CLI review of a frozen unit: read-only sandbox, ephemeral temp dir, strict output schema `{assessment, issues, escalation}`, effort medium/high/xhigh from risk flags, at most two attempts (one explicit escalation), refuses nested Codex. |
| Integration gate | `integrate.py` (18-350) | Enroll task + clean independent target worktree/branch; `freeze` rechecks identity and runs `git merge-tree` preflight (no target mutation); review keyed by integration ID; `approved` requires a confirmed routine result plus explicit acknowledgement of every risk; `merging` persisted before Git; `merged` records parents/tree/reachability receipt; `defer` with reason. |
| Templates | `templates/{task,learn,continue,review}.txt` | Frozen prompt shapes; hashes snapshotted at submission so an already-sent payload can never change. |
| Tests | `test_dispatch.py`, `test_lifecycle.py`, `test_batch.py`, `test_recovery.py`, `test_integrate.py`, `native_smoke.py` | Disposable Git repositories and a loopback fake API that injects durable events (`api.append`, `api.finish`), `ForkClient` for learning, and a fake-provider native smoke. 117 fixture tests plus the native smoke at the extension checkpoint (LIFECYCLE.md). Run: `python3 -m unittest test_dispatch test_lifecycle test_batch test_recovery test_integrate`. |

### 1.2 What the native session interface supports (verified from use)

- `GET /api/session/:id` (placement, model, agent, parent, workspace),
  `GET /api/session/active`, `GET .../inbox`, `GET .../form`.
- `GET /api/experimental/session/:id/log?after=N&follow=false` — durable SSE
  replay with `log.synced` watermark; the dispatcher never trusts prose or UI
  state, only durable events plus Git.
- `POST .../prompt` with client-chosen stable `id`, `delivery:"queue"`,
  `resume:true` — idempotent admission: repeating the same command returns the
  saved task without a second POST.
- `POST .../fork {}` — projected conversation copy in the same directory;
  `PATCH .../session/:fork` (title, permissions, archived) and
  `POST .../model` configure only the fork.
- Session moves/model changes surface as durable hazard events
  (`session.moved`, `session.model.selected`, ...), which `event()` records.

### 1.3 Verified operating state (2026-11-08, ledger `ledger.sqlite3`)

- 101 task rows: 50 `verified`, 50 `accounted` learning children, 1
  `completed` (MP-08 port readiness; unit `needs_review` for a
  three-commit delivery), 1 `uncertain` (a failed Big Pickle attempt with one
  `observing` continuation intent).
- 50 units `ready`, 2 `needs_review`; 42 queue rows `ready` (integration
  review backlog), 2 `deferred`, none pending/active.
- 17 integration enrollments, all `deferred`: the operator merged reviewed
  tips manually (see `integration/QWEN-INTEGRATION-20261107.md` in the audit
  workspace) instead of driving the integrate gate end-to-end.
- Feedback census: `edit_match_failed` 33 events/13 tasks,
  `tool_unavailable` 29/27, `permission_denied` 5/5, `provider_quota` 2/2.
- Representative success: the 2026-11-07/08 overnight batch
  (`batch-20261108-next`) ran three execution tasks unattended — admission,
  execution, Git verification, learning accounting, and `ready` units for all
  three (single commits, `learning: completed_no_changes`); the observer
  exited 0 at 04:17:34Z. Capacity behavior worth noting: the third task
  waited from enqueue (~23:35Z) until one executing task freed a slot
  (table8 verified 23:46:41Z, third task admitted ~23:49Z) because the
  completed-but-unverified, deferred MP-08 record (unit `needs_review`)
  conservatively reserved the third vLLM slot — without that hold, all three
  would have started together. A separate failed Big Pickle attempt (with an
  `observing` continuation intent) likewise reserves its own pool's slot
  under the same conservative rules (`batch.py:137-165`).

### 1.4 What is already reliable

1. Exactly-once admission with durable intent before POST; no automatic
   resend on uncertainty.
2. Event-cursor reconciliation with replay watermark validation; history gaps
   and foreign input become explicit `uncertain`/hazard states.
3. Git-scope verification (ancestry, path scope, trailer, cleanliness).
4. Worktree/branch/session reservations that survive restarts
   (`held_worktree`/`held_branch` unit indexes, task unique indexes,
   integration indexes).
5. Bounded capacity admission with explicit availability windows and
   structured quota/rate-limit pool pauses (cooldown never auto-re-enables).
6. Learning on a disposable fork that cannot touch the original session.
7. Checkpointed continuation with approval-hash handoff and effect checks.
8. Freeze-first integration with conflict preflight and explicit risk
   acknowledgement before any merge.

## 2. Manual-intervention inventory

Traced across the 13 lifecycle stages in the assignment, from observed
batches and the ledger. "Auto-able" marks what deterministic code can own
under an approved policy.

| # | Observed problem | Root cause / evidence | Existing support | Missing functionality | Frequency / impact | Recommended resolution (phase) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Operator re-approves every session at a stale pin before each batch | `submit` refuses on `local["head"] != registration["head"]` (`dispatch.py:375`); approvals pin HEAD at approval time | `approve` fully validates (clean, idle, binding, tail cursor); `bcc32d799d` added baseline acceptance for continuation only | Automatic re-approval of a *new* task on the same session/worktree when the advance is clean, linear, and touches no assigned paths | Every batch; ~5 min x N sessions + judgment load | Phase 1: `reapprove` policy check reusing `approve()` |
| 2 | Operator reads PLAN.md and selects which 3 tasks run | No task selector exists (README: "no autonomous task selector"); the plan is Markdown prose + tables | Ledger records every prior task/commit | Machine-readable plan artifact + eligibility evaluation | Every batch; highest single manual cost | Phase 3: plan artifact + `controller plan` |
| 3 | Operator hand-writes ~40-line prompts with pinned commits, evidence paths, and audit rules | Prompt construction is pure manual context assembly | `templates/task.txt` adds the delivery contract; session memory carries context | Prompt *composition* from plan item (bounded text) + inherited-context pointers; model-assisted drafting only for new follow-ups | Every task | Phase 3 (deterministic composition); Phase 5 (model-drafted follow-ups, never auto-dispatched) |
| 4 | Operator builds the exact `--allow` list per task | `allowed_paths` requires exact files | `allowed_paths` validation | Contract lives in the plan item (`contract.allowed`); enqueue reads it | Every task | Phase 3 |
| 5 | Operator chooses runner `--seconds`, launches the observer, records PID/sentinel | `run` is a bounded observer lifetime; nothing restarts it when a window extends | Observer pattern (pid/start_ticks/exit sentinel) is established in batch dirs | Controller owns the loop lifetime, restarts after crash (reconcile-first), and extends until plan completion or explicit stop | Every batch; failure mode: unobserved queue | Phase 4: controller loop |
| 6 | Stale queue/deferred records accumulate (42 `ready`, 2 `deferred`, 2 `needs_review` units, 1 `uncertain` task) | `ready` means reviewable, not integrated; deferrals hold reservations by design | Explicit `defer` with reason; `inference_held` release rules | (a) deterministic retirement of `ready` rows after operator merge (with integrated-at receipt); (b) a `capacity_holds` report naming each slot reservation and its release action | Recurring; blocked re-approvals and capacity | Phase 2 (report) + Phase 4 (retire command) |
| 7 | Failed/interrupted tasks need a manual `recovery plan` → hash review → `prepare` | Safety: checkpoint-specific effects check is operator-attested | `recovery.py` is complete and tested | Policy-scoped automatic plan+prepare for *clean* failures (no executed tools, no partial work), bounded attempts; partial effects stay human | Per failure; night-interruption risk | Phase 2: policy-gated auto-continuation |
| 8 | Rework is a manually authored "…-correct" task (e.g. `qwen-impact-mp-pool-wording-correct-20261106`, `oel8-localfix1-transaction-correct-20261106`) | Review findings are prose; nothing converts a finding into a bounded correction assignment | Task-ID uniqueness and `request_hash` prevent accidental collision | Deterministic failure classification (contract violation vs execution failure vs environment blocker) and, for the bounded cases, an auto-generated correction task referencing the failed verification; substantive findings stay human | Observed 4+ times | Phase 2 (classify), Phase 5 (correction tasks) |
| 9 | Capacity unavailable because of conservative/stale reservations (overnight batch: the third task waited ~14 min because a completed-but-unverified deferred record reserved the third slot; without the hold all three would have started together) | `inference_held` is deliberately conservative (`batch.py:137-165`); `needs_review` units with a fork count as busy | Explicit release conditions exist; the 20261107 fixes (`828963c7`, `e8b3945b`, `77a2c3ef`) already narrowed releases | Visibility: name every hold and the single action that releases it; policy option to release the *inference* reservation of a settled-but-held record whose settled-tail proof already holds (evidence-based, same proof as `inference_held`) | Recurring; delays admissions, never wrong | Phase 2 report; optional policy flag, default off |
| 10 | Delivery contract violations (3 commits where 1 is required) require manual deferral + review | `verify` fails on parent check; queue goes `blocked` | The violation is precisely detected and retained | Classification: *policy violation* (commit count, path scope) → human hold; *missing commit with report-only output* → bounded correction task; never auto-merge/amend | Observed 1+ (MP-08) | Phase 2 |
| 11 | Integration review backlog: 42 `ready` units; integrate gate enrolled 17 times but all `deferred`; actual merges were manual `git merge` of pinned tips | The gate requires Codex review + explicit risk acknowledgement; Codex availability/quota is operator-controlled | Gate mechanics are complete and tested; manual merges with frozen receipts work | (a) deterministic *integration handoff* report (freeze preflight over all `ready` units: clean-merge-ready / conflict / drift, with proposed target diff) so the operator reviews a consolidated list, not 42 individual units; (b) advisory review fallback (Phase 6) so absence of Codex does not block preparation | Recurring; the largest accumulated human cost | Phase 4 (handoff), Phase 6 (advisory review) |
| 12 | Codex unavailability blocks the automated review path | `review.run` requires the `codex` executable, refuses nested Codex, and consumes quota | Review is explicitly optional in the queue (units stay `ready`/`await_review`) | Advisory Qwen review fork using the existing `review.txt` template and output schema, recorded as advisory; Codex remains preferred and can be policy-required for xhigh risks | Per review; weekend/night risk | Phase 6 |
| 13 | Duplicate/superseded investigations are caught by operator memory, not the system | No cross-check of a new assignment against completed work | `request_hash` prevents same-ID reuse; worktree/branch reservations prevent concurrent collision | Plan-item eligibility: `done` (explicit `completed_by` or deterministic coverage check), `superseded` (plan cross-reference), `in_progress` (ledger match) | Rare but expensive when missed (the 20261106 batch carried a ready qwen task next to its deferred Big Pickle twin) | Phase 3 |
| 14 | Context recovery: blocked/stale sessions force new sessions that re-read the same corpus | Approval binds one session/worktree; a blocked session blocks its worktree until resolved | Persistent sessions retain accumulated context; learning forks preserve it | Eligibility prefers the *least-blocked* approved session with relevant context (plan `session` preference + fallback list); blocked-context sessions are named in the report | Per batch when a session is blocked | Phase 3 (preference, not migration) |
| 15 | Task outputs that name a "next gate" become manual work | Reports are prose; follow-ups are not structured | PLAN.md continuation queue captures them after operator triage | Bounded follow-up reconciliation: scan report for known plan-item IDs (deterministic); unmatched proposals are recorded, optionally model-drafted into plan *candidates* for operator approval; never auto-dispatched | Per task with follow-ups | Phase 5 |
| 16 | Per-batch bookkeeping: batch dir, `learning-policy.json` copy, `dispatch-state.json`, observer files | The batch dir convention is manual | Files are small and conventions are stable | Controller creates one state dir per plan run; learning policy referenced from the plan (snapshotted as today) | Every batch | Phase 4 |
| 17 | Session placement drift (cwd vs worktree) stops submission; recovery is manual | Native `session.moved` changes `location.directory`; binding check is exact | Drift is detected at submit/approve and recorded as hazard | Re-approval (Phase 1) also covers verified moves: the controller re-approves the session at its new placement when the worktree/branch are unchanged and clean | Rare | Phase 1 |
| 18 | Registry accumulates (61 entries incl. ~50 archived `learn_*` child sessions) | Learning children are registered like tasks | Child rows become `accounted` and release indexes | Cosmetic: registry listing filter in reports; no migration needed | Low | Phase 2 (report filter) |

Genuine human gates that must remain human (not incidental friction):
plan authorization (commit), integration approval and merge, acceptance of
execution effects with partial/external side effects, pool re-enablement after
quota (explicit availability check), new scope beyond the plan, and any
production/external action. Everything else above is orchestration.

## 3. Target-state architecture

### 3.1 New artifacts (all stdlib Python + the same ledger)

| Artifact | Location | Purpose |
| --- | --- | --- |
| Plan artifact | Project repo, `dispatch/plan.json` (git-committed) | The authorized unit of work. Commit = authorization. Controller reads it read-only at a pinned commit. |
| `controller.py` | `script/session-dispatcher/` | The persistent autonomous controller: plan evaluation, re-approval, admission, continuation policy, classification, handoff report, loop lifetime. |
| `planctl.py` (or `controller.py plan/...`) | same | Read-only plan/eligibility reporting ("plan-next", the command proposed in RESOURCE_CONTROL.md). |
| Report dir | `~/.local/state/opencode/session-dispatcher/run-<planrun>/` | Per-run state: created batch files, observer pid/sentinel, consolidated report (JSON + Markdown), decision log. |

### 3.2 Plan artifact schema (v1)

```json
{
  "version": 1,
  "objective": "Bounded goal this run advances",
  "policy": {
    "max_auto_admissions": 6,
    "continuations": {"max_per_task": 1, "allow_clean_failures": true,
                       "allow_partial_effects": false},
    "reapprove": {"linear_advance": true, "placement_move": true},
    "review": {"preferred": "codex", "fallback_advisory": "qwen_fork",
                "require_codex_for": ["source_ambiguity", "memory_safety",
                                       "conflicting_evidence"]},
    "auto_merge": false
  },
  "items": [
    {
      "id": "mp04-primary-evidence",
      "title": "MP-04 primary-evidence repair",
      "status": "approved",
      "priority": 1,
      "depends_on": [],
      "classification": "prerequisite",
      "authorization": "auto",
      "session": ["qwen-f01-pilot", "qwen-oel6-mp04"],
      "worktree": "p2-pool2-wrapper-clear",
      "deadline": "PT12H",
      "contract": {
        "allowed": ["integration/mp04-primary-evidence/README.md",
                     "integration/mp04-primary-evidence/inputs.json",
                     "integration/mp04-primary-evidence/results.json",
                     "integration/mp04-primary-evidence/evidence.tar.gz",
                     "integration/mp04-primary-evidence/artifact-sha256.json"],
        "min_commits": 1, "max_commits": 1,
        "report_only_outcome": false,
        "readme_max_lines": 100
      },
      "context": ["mp04/README.md", "BUILD.md", "PODMAN_ACCESS.md"],
      "prompt": "Bounded assignment text (the existing prompt style)"
    }
  ]
}
```

Rules:

- `status`: `approved | deferred | superseded | done | needs_authorization`.
  Only `approved` items are eligible; `done` items may carry
  `completed_by: <task_id>` or a path-coverage check.
- `authorization`: `auto` (within plan scope: read-only research, bounded
  tests in approved diagnostic environments, documentation, checkpoints,
  commits in the assigned worktree), `policy` (requires a policy flag set in
  the plan), `human` (controller never admits; reports only).
- `depends_on` names item IDs; prerequisites are `done` when their
  `completed_by` task is `verified` (or the dependency is marked
  `done` in the plan).
- Stable identifiers: item ID → task ID (`<item-id>[-attempt]`) → commit →
  review status, all recorded in the ledger and report for traceability.
- The controller never edits the plan. Plan changes are new commits; a
  controller run is bound to the plan commit it started with. In-flight
  items keep their frozen spec (existing `request_hash` semantics).

### 3.3 Eligibility engine (deterministic)

For each plan item, the controller computes exactly one state:

| State | Determination (all checks verifiable from ledger + Git) |
| --- | --- |
| `done` | Plan `status=done`, or `completed_by` task is `verified`, or (opt-in per item) path coverage: some `verified` task's proof paths intersect the item's `contract.allowed` and its commit is an ancestor of the item worktree's HEAD. |
| `in_progress` | A task row (any state not `verified/accounted`) or an `active/continuing/blocked` queue row maps to the item. |
| `superseded` | Plan `status=superseded` (with `superseded_by`). |
| `blocked_prereq` | Any `depends_on` item not `done`. |
| `needs_authorization` | `authorization=human` or `policy` without the policy flag. |
| `unapproved` | Plan `status=deferred/needs_authorization`. |
| `session_unavailable` | No approved session for the item's worktree, or every candidate session is blocked (`needs_review`/`uncertain` reservation). |
| `eligible` | `status=approved`, prerequisites done, a candidate session exists, pool window open. |

States are determined in table order (first match wins): a `done` item is
never reported as `in_progress` even if a stale row exists, and an
`in_progress` item is never re-evaluated for prerequisites. `blocked_human`
(§3.5) is the queue-level `blocked` state with a human-gated issue, not a
plan-item state.

Eligible items are admitted in `priority` order, subject to pool capacity
(existing `availability`), the plan's `max_auto_admissions` budget, and
per-session/worktree exclusivity (existing indexes). Admission reuses
`batch.enqueue` + `batch.tick` unchanged.

### 3.4 Execution loop (controller tick)

Each tick, under the existing ledger lock, in order:

1. **Reconcile**: `batch.tick()` (active observation, learning advancement,
   quota pauses, phase reporting) — unchanged.
2. **Classify** newly settled tasks (Phase 2):
   - `verified` → item `done`-candidate; release; report.
   - verify failure with commit present → classify the exact failure
     (parent/commit-count, path scope, trailer): *policy violation* → hold
     for human (queue `blocked` with a named issue, MP-08 style); *missing
     commit* with `report_only_outcome` true → accept as documented negative;
     otherwise → bounded correction task (Phase 5) or human if the violation
     is not in the auto-correction set.
   - terminal failure/interruption, clean (no executed tools, no partial
     work, settled tail — the exact preconditions `recovery.plan_locked`
     already enforces) and within `continuations` budget → automatic
     `recovery.plan` + `prepare` (same model, same session), recorded in the
     attempt table. Partial effects or budget exhausted → human.
3. **Re-approve** (Phase 1): for each eligible item whose candidate session's
   registration is stale, run the re-approval policy check and call the
   existing `approve()`:
   - worktree clean; session idle with settled tail (existing `approve`
     checks);
   - HEAD advanced linearly from the registered base (ancestry check), and
     the diff since the base touches none of the paths assigned to that
     session's still-unresolved tasks (the same guard
     `recovery.plan_locked(..., accept_base)` uses at its baseline
     acceptance);
   - placement move: allowed only when branch/common-dir are unchanged and
     the worktree is clean (friction #17).
   Re-approvals are recorded in the decision log (old/new base, reason).
4. **Admit** pending-eligible plan items within remaining budget.
5. **Report**: write the consolidated report (below) and the decision log.

### 3.5 Stop conditions and boundedness

The controller run ends (observer-lifetime semantics, as today) when:

- all plan items reach `done / superseded / unapproved / needs_authorization /
  blocked_human` (no eligible work remains), or
- its `--seconds` bound expires, or
- the pool window closes with only pending work (`paused`), or
- a queue row is `blocked` with a human-gated issue (controller keeps
  observing admitted work but admits nothing new — existing
  `phase=blocked` behavior), or
- the `max_auto_admissions` budget is exhausted.

Guarantees retained from the existing design: intent-before-POST (no
duplicate prompts after crash), reconcile-first restart, no automatic resend
of uncertain admissions, no cancellation of running work by stopping the
observer, explicit-only pool re-enablement, no merge without explicit
approval.

### 3.6 Consolidated report (per tick, final on exit)

Written to the run state dir; one Markdown + one JSON:

- **Plan board**: each item → state, reason, task IDs, commits, session,
  next action (including the *exact single command* that releases a hold).
- **Units ready for integration**: per unit — source tip, target,
  merge-tree preflight verdict (clean/conflict/drift), review state
  (none/advisory-confirmed/codex-confirmed), evidence refs. This is the
  operator's review surface; it replaces per-unit manual inspection.
- **Blockers and human gates**: every `blocked/needs_review/uncertain` row
  with cause and the minimal action; every `capacity_holds` entry (which
  unit/task holds a slot, why, release action).
- **Follow-up proposals**: plan-item IDs referenced by reports (deterministic
  scan) + unmatched proposals recorded for plan review.
- **Budgets and windows**: admissions used, pool states, remaining window.

### 3.7 Where the model is and is not used

Deterministic code owns: eligibility, ordering, admission, re-approval,
continuation planning under policy, classification, verification, reporting,
reconciliation. A model is permitted in exactly three bounded roles, each
schema-checked and never crossing an authorization line:

1. **Drafting** a new plan-item candidate from an unmatched follow-up
   proposal (output: a plan-item JSON *draft* for operator commit; never
   auto-dispatched).
2. **Advisory review** of a frozen unit when Codex is unavailable (Phase 6;
   same template, same schema, advisory flag; merge approval still requires
   the existing confirmed-routine + explicit risk-acknowledgement flow and
   human approval).
3. **Drafting** a bounded correction prompt for a classified
   *missing-delivery* failure (Phase 5), which is then admitted only if the
   corresponding plan item (pre-declared `correction_of`) exists and is
   `approved` with `authorization=auto`.

No model call selects work, releases capacity, re-approves sessions,
continues a task, or approves a merge.

**Untrusted content.** Agent reports, patches, logs, fetched content, and
reviewer output are data, never instructions: the existing templates already
state this for task/learning/review prompts, and the controller reads agent
output only through deterministic, bounded channels (path/ID scans of
verified-commit files, schema-validated reviewer JSON). A plan item can never
be created, modified, or admitted by anything an agent wrote; plan changes
require an operator commit. Prompt injection in retained evidence can at most
waste a bounded execution and surface in the report — it cannot change
authorization, capacity, or scope, because none of those decisions consume
untrusted input.

## 3.8 Approach comparison (A/B/C)

| Approach | What it adds | Verdict |
| --- | --- | --- |
| **A. Incremental dispatcher improvements** (fill gaps in queue/admission/reconciliation/state handling) | Auto re-approval (Ph 1), settlement classification + policy auto-continuation (Ph 2), hold reporting/retirement (Ph 2/4). | Necessary but insufficient alone: with no plan source, the operator still selects work, writes prompts, and assembles each batch. |
| **B. Persistent autonomous controller** (repeatedly evaluates project state, selects eligible work, monitors outcomes, transitions tasks through the approved workflow) | Plan artifact + eligibility engine + plan-driven admission + loop lifetime + consolidated handoff (Ph 0, 3, 4). | The core of the target experience; every decision is deterministic and auditable in the decision log. |
| **C. Model-assisted planning with deterministic execution** (model proposes, code enforces) | Drafting new plan-item candidates from follow-ups, advisory review fallback, correction-prompt drafting (Ph 5, 6; §3.7 roles). | Valuable only in the three bounded roles; using a model for operational decisions (selection, admission, continuation, approval) would add failure modes without removing any existing one, and is rejected. |

**Recommended combination: B built on A, with C in its three bounded
roles.** This is the least-complexity path to the target experience because
B's machinery largely already exists (admission, reconciliation, learning,
continuation, integration preflight); the genuinely new code is the plan
artifact, the eligibility engine, re-approval, classification, and the
loop/report — exactly Phases 0-4. Each phase is independently committable,
so the combination degrades gracefully: after Phase 3 alone the operator
still gets unattended *batches*; Phases 4-6 then remove the remaining
night-time intervention points (crash restart, integration backlog, Codex
availability).

## 4. Implementation phases

Each phase is independently committable and testable with the existing
fixture suite. Schema migrations follow the existing opt-in versioning
(user_version bumps inside `BEGIN IMMEDIATE`, older clients refuse newer
ledgers, live ledgers never auto-migrated).

### Phase 0 — Plan artifact + read-only eligibility (`controller.py plan`)

- **Behavior**: validate and load `dispatch/plan.json` (schema v1); compute
  the §3.3 eligibility table against the live ledger and Git; print the plan
  board. No POSTs, no state writes (the `plan-next` proposal from
  RESOURCE_CONTROL.md).
- **Code areas**: new `controller.py` (~250-350 lines: schema validation,
  eligibility, report rendering); reuses `dispatch.Dispatcher` read paths.
- **Schema/migration**: none (ledger v5 unchanged; plan is repo data).
- **Tests**: fixture repo + ledger: item done via `completed_by`; done via
  path coverage; in_progress for each non-terminal task state; superseded;
  blocked_prereq (including a two-level chain); needs_authorization;
  session_unavailable (blocked session reserves worktree); plan changed under
  the controller (hash mismatch → refuse, no stale admission); malformed
  schema cases (unknown keys, bad status, cycle in `depends_on`, duplicate
  IDs, allowed-path reuse across concurrent items on one worktree).
- **Acceptance**: `controller plan --plan <ref>` against the live 2026-11-08
  ledger reproduces the manually determined states of the current backlog
  (42 ready units, 2 deferred holds, 2 capacity-holding records) with zero
  writes to the ledger (verified by file mtime/hash before/after).
- **Dependencies**: none. **Backward compat**: pure addition.
- **Failure/rollback**: none at risk (read-only); delete the file to roll back.

### Phase 1 — Automatic re-approval

- **Behavior**: `reapprove` helper: given a registry name, verify
  §3.4(3) conditions, call `Dispatcher.approve`, record old/new base in the
  decision log. Exposed to the controller (Phase 4) and as a manual
  `controller.py reapprove <name>` for the operator.
- **Code areas**: `controller.py` (+~120 lines); calls existing
  `dispatch.approve`/`identity`/`git`.
- **Schema/migration**: none (registry rows are already upserted).
- **Tests**: stale pin on linear advance → auto re-approve; advance touching
  assigned paths → refuse (message names the paths); dirty worktree →
  refuse; non-idle session → refuse; placement move with clean worktree →
  re-approve (policy flag) vs refuse (default off); branch change → refuse;
  concurrent re-approval (two controllers) → one wins via existing unique
  reservation; re-approval never happens for a session with an unresolved
  task (existing check at `dispatch.py:327` is the guard — test it holds).
- **Acceptance**: in the fixture batch flow, a second task on the same
  session/worktree is admitted by the queue *without* a manual `approve`
  after the first task's unit becomes `ready`; the ledger registry shows the
  renewed base.
- **Dependencies**: Phase 0 (uses the decision log). **Backward compat**:
  manual `approve` unchanged. **Rollback**: the helper is unused by other
  phases until Phase 4.

### Phase 2 — Settlement classification + auto-continuation policy + hold reporting

- **Behavior**:
  - `classify(task)`: map a settled/failed task to
    `verified | report_only | policy_violation | clean_failure |
     partial_effects | environment_blocker`, from `verify` outcomes, terminal
    state, tool-event evidence, and hazard/issue text (deterministic
    matching on the exact strings the ledger already stores; no free-text
    inference).
  - Auto-continuation: for `clean_failure` within policy budget, run
    `recovery.plan` + `prepare` unattended (model = original), recording the
    attempt; `partial_effects` → `blocked` with the exact
    `--effects-checked` attestation still required from a human.
  - `capacity_holds` report section: enumerate every session counted by
    `availability` (native active, `inference_held` tasks, learning
    reservations) with the single action that releases each.
  - `controller.py retire <task> --integrated-at <sha>`: record a human
    integration receipt on a `ready` queue row and mark it retired
    (new queue state `retired`, additive; keeps the row as history).
- **Code areas**: `controller.py` (+~200 lines); `recovery.py` unchanged
  except a small `prepare_auto` wrapper that passes the policy-attested
  `effects_checked=False` path for clean failures only; `batch.py`
  `save()` gains the `retired` state (additive constant).
- **Schema/migration**: ledger v5 → v6 (additive: queue state constant
  accepted by existing code via its `WHERE state=...` lists — audited;
  `attempt` table already exists when recovery is used). Older clients
  refuse v6 only if the constant appears where they validate strictly;
  keep the constant out of `queued.state` default paths that old clients
  read, or ship the version bump with the same commit as controller
  adoption. (Decision: bump to v6 in this phase; document the refusal.)
- **Tests**: each classifier branch with fixture event sequences (clean
  failure before any tool event; failure after executed tool; partial work in
  allowed paths; partial work outside allowed paths; three-commit success;
  missing commit; trailer missing); auto-continuation fires once and not a
  second time (budget); partial effects never auto-prepare; `retire` on a
  non-ready row refuses; `retire` preserves the unit receipt;
  `capacity_holds` lists the fixture's held session with correct release
  action; migration v5→v6 fixture; old-client refusal test.
- **Acceptance**: a fixture task that fails cleanly at step N resumes
  unattended through `continuing → active → verified` with exactly one
  continuation attempt; a task failing after an executed tool stays
  `blocked` with the attestation named; the report names the fixture's
  deferred hold and its release command.
- **Dependencies**: Phase 0. **Backward compat**: `recovery` CLI unchanged;
  queue state constant additive. **Rollback**: disable via plan policy
  (`continuations.max_per_task: 0`).

### Phase 3 — Plan-driven admission (the first slice's core)

- **Behavior**: `controller start --plan <path@commit> --seconds N`:
  load the frozen plan; each tick run §3.4 (reconcile → classify →
  re-approve → admit → report). Admission creates the batch state dir,
  enqueues via `batch.enqueue` with the item's contract (allowlist,
  learning policy reference snapshotted exactly as today), and admits within
  `max_auto_admissions`.
- **Code areas**: `controller.py` (+~250 lines); `batch.py` unchanged.
- **Schema/migration**: none.
- **Tests**: end-to-end fixture (§5) — this phase is what the acceptance
  test exercises.
- **Acceptance**: §5 acceptance test passes with zero manual
  approve/enqueue/runner commands; operator actions limited to committing
  the plan and starting/stopping the controller.
- **Dependencies**: Phases 0-2. **Backward compat**: manual flow untouched.
- **Failure/rollback**: controller exits leave the ledger in a valid
  reconciled state (existing invariant); stop = stop admitting, keep
  observing.

### Phase 4 — Controller loop lifetime + consolidated handoff

- **Behavior**: crash-safe loop (pid/sentinel files in the run dir,
  reconcile-first restart, bounded `--seconds`, clean exit codes reusing
  `run` semantics); integration handoff: for every `ready` unit, run the
  `integrate.freeze`-equivalent preflight (fresh checks + `merge-tree`
  against the operator-designated target) **read-only** and report
  clean/conflict/drift with the proposed target diff; never enroll or merge.
- **Code areas**: `controller.py` (+~150 lines); reuses `integrate.fresh` /
  `freeze` logic by extraction (move the merge-tree preflight into a shared
  read-only helper in `integrate.py`; no behavior change).
- **Schema/migration**: none (no enrollment; preflight creates Git objects
  only, as `freeze` documents).
- **Tests**: restart after SIGKILL mid-tick → no duplicate admissions
  (message-ID idempotency); handoff over two `ready` units with a
  conflict between them and the target → conflict named with paths; target
  drift after preflight → drift named; preflight never writes refs
  (assert `MERGE_HEAD`/index untouched).
- **Acceptance**: overnight fixture run (§5) produces the final report with
  per-unit preflight verdicts; a second controller start on the same run dir
  resumes without re-admitting anything.
- **Dependencies**: Phase 3. **Rollback**: report-only mode flag.

### Phase 5 — Follow-up reconciliation + bounded correction tasks

- **Behavior**: deterministic scan of delivered report files for plan-item
  IDs (`contract.allowed` paths, read from the verified commit); unmatched
  proposal text is recorded verbatim (never interpreted); optional
  model-drafted plan-item *candidates* (role C-1) written to the run dir for
  operator commit; bounded correction tasks for `missing-delivery`
  classifications using pre-declared `correction_of` items (role C-3),
  admitted only under `authorization=auto` with a fresh single-commit
  contract.
- **Code areas**: `controller.py` (+~150 lines); template
  `templates/correction.txt` (new; same contract style).
- **Schema/migration**: none.
- **Tests**: report referencing a known ID → `planned` (no new task);
  referencing a `done` ID → `already_completed`; unmatched text → recorded,
  not dispatched; correction task admitted once, verified, then item
  `done`; correction violating single-commit → Phase 2 hold (no retry).
- **Acceptance**: a fixture batch whose first task reports "next gate: item
  B" results in item B (already `approved` in the plan) being admitted
  without operator action; an unknown proposal appears only in the report.
- **Dependencies**: Phases 2-3. **Rollback**: policy flag off.

### Phase 6 — Advisory review fallback (Codex-independent preparation)

- **Behavior**: when `review.preferred` (Codex) is unavailable (executable
  missing or prior run uncertain) and the unit's risks are not in
  `require_codex_for`, run one advisory review on a disposable fork:
  deny-by-default permissions + read-only, single scratch output path
  (untracked, 600, removed after validation), `templates/review.txt`
  unchanged, result validated against the existing schema and stored in the
  `review` table with an added `source` column (`codex|qwen_fork`) and
  `advisory=true`. `integrate.approve` behavior is unchanged: it still
  requires a confirmed routine result and explicit risk acknowledgement, and
  the human still approves; advisory results are marked in the handoff
  report.
- **Code areas**: `review.py` (+~120 lines: fork-based runner mirroring the
  learning fork machinery in `lifecycle.py`; `schema` + validation reuse);
  ledger v6 → v7 (additive `review.source`/`advisory`).
- **Tests**: fork review happy path (structured JSON accepted); malformed
  result rejected (row `uncertain`, no retry); scratch path removed;
  `require_codex_for` risk → fallback refused, unit stays `await_review`;
  advisory result never satisfies `integrate.approve` by itself without the
  existing confirmed-routine check (assert the existing guard reads any
  source); nested-Codex refusal still applies to the codex path.
- **Acceptance**: with `codex` absent from PATH, a fixture unit reaches
  `await_approval` through the advisory path in the handoff flow, and the
  report labels it advisory; with an xhigh risk it stays `await_review`.
- **Dependencies**: Phases 2, 4. **Rollback**: policy flag
  `review.fallback_advisory: null`.

## 5. Autonomous-operation acceptance test

Fixture-based, disposable, no live sessions/models/containers, using the
existing `DispatcherTests` fake API and `ForkClient` patterns. One scenario,
one committed plan, one controller run:

**Setup**: disposable repo; plan v1 with items:
`A` (auto, priority 1, allowed `a/README.md`),
`B` (auto, priority 2, `depends_on: [A]`, allowed `b/README.md`),
`C` (auto, priority 3, allowed `c/README.md`, policy: clean-failure
continuation allowed),
`D` (human), `E` (auto, `completed_by` a pre-existing verified task → must
be `done` without dispatch), `F` (auto, duplicate of E's paths → must be
`done` via path coverage). Two approved sessions/worktrees (S1 for A, C;
S2 for B, F). Pool capacity 2, window open; learning policy with a
`completed_no_changes` fixture.

**Sequence and assertions** (one controller `start`, bounded seconds):

1. A and C admitted tick 1 (capacity 2); B blocked_prereq; D
   needs_authorization; E/F done (no task rows created) — report asserts
   all six states.
2. A completes with a valid single commit → verified → unit ready →
   learning no-op; S1 re-approved automatically at A's commit (Phase 1
   asserted: registry base advanced, decision-log entry).
3. B admitted after A's unit is ready (slot + re-approval); C's session
   fails cleanly (fixture: `session.execution.failed` before any tool
   event, settled tail) → auto continuation (one attempt) → resume prompt
   delivered once (message-ID idempotency asserted) → completes → verified.
4. Controller process SIGKILLed while B is active; restarted → reconciles
   from cursor; no second prompt for B (asserted via inbox count); B
   completes → verified.
5. A second task on S1 after A: admission uses the renewed approval (no
   manual `approve` in the run — asserted by counting approve calls).
6. Invalid delivery: fixture commits *two* commits for a scratch item G
   (added to plan, auto) → verify parent check fails → classified
   `policy_violation` → queue `blocked`, human hold named in report; no
   auto-retry, no amend.
7. Capacity exhaustion: set pool capacity 1 mid-run → remaining eligible
   item waits; window close → phase `draining` then `paused`; controller
   exits code 2 with active work still observed (asserted).
8. Stale revision: operator commits directly to S2's branch (clean, linear,
   no assigned paths) before B's second use → controller re-approves
   automatically; a conflicting advance (touching B's allowed path) →
   refuse + named blocker.
9. Final report: plan board (A/B/C/G verified-or-held, D human, E/F done),
   integration handoff (A/B/C units: merge-tree preflight verdicts against a
   disposable target worktree; one forced conflict named), capacity holds
   (empty or named), follow-up scan (A's report mentions item B's ID →
   `planned`, not re-dispatched), budgets (admissions ≤ cap).

**Pass criteria**: the entire scenario completes with exactly the model
promits asserted above (no more, no fewer), zero manual
approve/enqueue/recover/retire invocations, the ledger in a fully
reconciled state, and the final report matching the asserted plan board.
The scenario is committed as `test_controller.py` and runs in the existing
`python3 -m unittest` suite.

## 6. Recommended first implementation slice

**Phase 0 + 1 + 3 together** (plan artifact, automatic re-approval,
plan-driven admission), because that combination alone delivers the target
operator experience for *well-formed* batches:

- Operator commits `dispatch/plan.json` (the authorization).
- `controller.py start --plan dispatch/plan.json@<sha> --seconds <window>`
  is the only other command.
- Operator returns to: verified units (each with its commit and evidence
  paths), the consolidated plan board, and named blockers.

**Exact acceptance criteria**:

1. `controller plan` reproduces the current live backlog states (from the
   2026-11-08 ledger snapshot: 42 ready, 2 deferred holds, 2
   capacity-holding records) read-only (ledger file hash unchanged).
2. A fixture plan with 3 items on 2 sessions admits and verifies all three
   in one unattended run with zero manual approve/enqueue commands,
   including the automatic re-approval of the session whose base advanced
   after item 1.
3. Every controller decision (admit/reapprove/hold/report) is present in the
   run-dir decision log with its evidence (task ID, base, reason string).
4. Stopping the controller (SIGTERM) leaves the ledger valid: no lost
   intent, no duplicate admission on restart; a blocked item never admits
   while a queue row is `blocked`.
5. The manual flow (`dispatch`/`batch`/`lifecycle` CLIs) is byte-for-byte
   unchanged in behavior (the existing fixture suite, 117 distinct tests at
   the last recorded checkpoint, stays green and unmodified).
6. No new dependency, service, database, or ledger table beyond Phase 0-3
   (none); no OpenCode-core change.

Estimated size: ~650-800 lines of new Python + tests; no OpenCode-core or
API changes; two of the three phases are pure additions.

## 7. Remaining decisions (operator input required)

| # | Decision | Recommended default |
| --- | --- | --- |
| 1 | Plan artifact location and authority | `dispatch/plan.json` in the project repo; commit = authorization; controller read-only. |
| 2 | Auto-continuation budget | 1 clean-failure continuation per task, same session/model; partial effects always human. |
| 3 | Auto-merge | Off, permanently by default; the controller only prepares the handoff. Merge stays a human action through the existing gate or manual merge. |
| 4 | Advisory review fallback (Phase 6) | Allowed for non-xhigh risks when Codex is unavailable, always labelled advisory; xhigh risks wait for Codex or a human. |
| 5 | Pool windows | Standing per-plan window in the plan (`policy`), explicit re-enable after quota still required (no change to the existing rule). |
| 6 | Retiring `ready` rows after manual merge | `controller retire --integrated-at <sha>` receipt; keep all 42 current rows until individually retired (no bulk migration). |
| 7 | Releasing inference holds of `needs_review` learning units | Policy flag, default off (visibility only in Phase 2); enable only after the hold report proves the settled-tail evidence in practice. |
| 8 | Where the consolidated report is committed (if at all) | State dir by default; optional git commit to a dedicated reporting branch is a later decision. |

## 8. Code observations (documented, not changed)

- `batch.py:130-134` counts `needs_review` units with a `fork_id` as busy
  indefinitely. This is the conservative reservation observed in the
  20261108 batch. A fixture test should pin that behavior (currently only
  indirectly covered by `test_batch.py` capacity tests) so any future
  release change is deliberate.
- `dispatch.py:290-293` (`Dispatcher.update`) silently no-ops updates on
  `verified/accounted` rows; `lifecycle.advance_locked` relies on the unit
  stage, not the task state, for terminality. A comment or assertion would
  prevent a future caller from assuming the update applied.
- `review.run`'s `review` table has no `source` column; Phase 6's additive
  v7 migration should backfill `source='codex'` for existing rows.
- The `queued` table's `deferred` state is set by direct SQL in operating
  practice (no CLI command); a `controller.py defer <task> --reason`
  (wrapping the existing update) would make the deferral protocol a
  documented command, matching RESOURCE_CONTROL.md.
- Operational note on the fixture suite: `test_integrate` exercises *real*
  `git merge` (`integrate.apply_locked`, `integrate.py:228-232`). Under a
  restrictive sandbox (e.g. the `opencode-safe` wrapper) that blocks the
  `merge` subcommand, those tests fail even though the code is correct —
  the seven `test_integrate` merge tests fail while the other 127 pass, and
  a minimal reproduction shows `git merge` blocked but read-only
  `git merge-tree --write-tree` succeeding. This is consistent with, and
  reinforces, the Phase 4 split: the *handoff preflight* is read-only
  (`merge-tree`) and can run unattended, while the actual merge remains a
  gated, operator-run mutation. It is not a code defect and is not changed
  here.

Nothing in this document changes any of the above; each item ships with the
phase that owns it, with its test.
