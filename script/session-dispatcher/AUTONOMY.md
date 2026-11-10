# Unattended operation: current capabilities and delivery milestones

Reviewed 2026-11-10 against dispatcher commit
`d59c11f64eb56f1c9fdf64f858fd0fe1a8585b88`, its tests, the running ledger
(read-only), and the recent Qwen batches. This replaces the initial Qwen design
at `64e8d9e656` and its 2026-11-09 amendment; their full rationale remains in Git.
It is an implementation roadmap, not authorization to change live assignments.

## Assessment

The goal is feasible by extending the existing dispatcher. It already drains a
deep **approved** backlog through execution, Git verification and learning.
Repeatedly supplying only three tasks was an operating practice, not a queue
limit. M1 below now supplies opt-in runner/status support. The remaining work is
safe context reuse, recovery policy and review preparation. A new general controller, task database
or inference client would duplicate working behavior.

Automatic admission and selection of new technical investigations are different
problems. Deterministic code can drain a finite approved work list; deciding what
new correction or investigation is worthwhile still needs technical judgment.
The first useful target is an unattended approved backlog ending in reviewable
units and named blockers. Integration keeps its existing human approval.

## What works, and what still needs work

| Capability | Current implementation and evidence | Remaining boundary |
| --- | --- | --- |
| Persistent sessions and source identity | `dispatch.py`: explicit registry pins session, model, agent, canonical worktree/branch/base and retained event cursor. | No automatic discovery/allocation or registry renewal. A clean checkout and native directory metadata alone do not establish ownership; manually moved sessions have made those disagree. |
| Queued delivery and observation | `batch.py`: deep frozen backlog, stable task/message IDs, intent before one POST, durable replay, Git verification and assignment-scoped error handling. Recent live batches use it. | Admission deduplication does not prove tool effects happened once. Observer restart reconciles existing intent; it does not resend uncertain work. |
| Resources and hours | Provider pools enforce capacity, external occupancy, finite admission deadline, quota/rate-limit pauses; learning shares capacity. | No recurring calendar, production-GPU detector, cross-host/provider-alias accounting or enforced container/build leases. Those remain explicit coordinator inputs. |
| Post-task learning | `lifecycle.py`: once-only disposable fork, Qwen override, existing learn prompt snapshot, optional separate commit, failure/no-change accounting and archival. Live use plus native fixture smoke. | The fork shares the task's filesystem. Keep the worktree reserved through accounting. Pre-admission wait/skip is bounded; an uncertain executing fork cannot simply time out and release ownership. |
| Dependencies | `dependencies.py`: immutable task-ID edges, `verified` / `integrated` / `approved`, admission proofs, cycle checks and scoped holds. Included in the recorded 159-fixture pass at `d59c11f64e`. | Opt-in schema 6 is implemented; the inspected live ledger is still schema 5 without edges. It was not migrated in this review. |
| Interrupted-task continuation | `recovery.py`: checkpoint/effect evidence, approval hash, one durable continuation intent, deliberate model handoff and capacity waiting. Big Pickle quota and Qwen continuation have live operating evidence. | Planning/preparation and quota reopening need explicit approval. No standing retry policy or unattended effects judgment exists. |
| Review and integration | `review.py` and `integrate.py`: frozen unit/target, merge-tree preflight, structured bounded review, explicit approval and history-preserving merge receipt. | Fixtures cover the mechanics; the live record has 20 deferred integration enrollments because coordination used manual reviewed merges. This is not 20 outstanding unmerged results. No paid end-to-end review is claimed by the fixture pass. |
| Prompts and feedback | Versioned task/learn/continue/review templates; stored hashes and exact submitted payloads; event-linked feedback. Latest task assignments explicitly avoid routine questions. | That instruction currently lives in batch text, not a shared template. No automatic question responder or prompt rewriting exists. |

Version observations: Python 3.14.4, Git 2.53.0, Bun 1.4.4, installed OpenCode CLI
v2.2.2 and Codex CLI 0.167.0. The dedicated host runs the source
[serve.ts](serve.ts), not necessarily the installed CLI build; preserve its
existing retained-event contract rather than inferring compatibility from a
CLI version. The review inspected implementation/tests and existing results;
it did not rerun model work, migrate the ledger or restart the active observer.

The [README](README.md) owns commands and schema contracts;
[LIFECYCLE](LIFECYCLE.md) owns fork/review/guard details;
[resource design](RESOURCE_CONTROL.md) retains historical measurements and
broader proposals. PLAN remains the project's technical roadmap. The private
SQLite ledger owns assignments, dependencies, approvals and operational state;
HANDOVER should retain decisions, technical blockers and recovery pointers.

## Corrections to the original proposal

- **A plan commit is provenance, not approval.** An agent-authored plan cannot
  authorize itself. Import requires an explicit operator-approved scope or
  standing policy, bound to the exact input digest. Runtime state belongs in
  SQLite; do not add a Git-maintained `approved/done/in_progress` board.
- **Never infer completion from shared output paths.** A verified task commit
  may be a blocker checkpoint. `verified` proves Git/event provenance plus
  accounted learning, not that a technical prerequisite passed. Choose
  `approved` where acceptance requires judgment; later structured outcome
  evidence must not weaken that distinction.
- **Dependencies already exist.** Use the actual task-ID contract below, not
  the old plan-item `depends_on` sketch or a second eligibility engine.
- **Holds are scoped.** Failed/deferred prerequisites affect descendants;
  independent work continues. Provider closure and conservative inference
  reservations can still limit a whole pool. Do not release an uncertain
  reservation merely to make progress.
- **Reapproval needs a known result and intended base.** Linear ancestry and
  disjoint output paths are insufficient: source inputs, instructions or API
  contracts may have changed. Do not accept session moves or retarget inputs
  merely because the new worktree is clean.
- **Do not retry missing deliverables blindly.** A missing commit, interrupted
  build or absent terminal event may coexist with completed tool effects.
  Preserve the current checkpoint and classify it before drafting rework.
- **Avoid unconditional guard claims.** Learning sends a literal snapshot of
  the existing command prompt, not the native slash-command processor.
  `edit`, `write` and `patch` share permission action `edit`; the dedicated-edit
  requirement is checked from tool events after execution, not fully enforced
  at that permission boundary. See LIFECYCLE's recorded limitation.

## Dependency adoption

The [implemented contract](README.md#task-id-prerequisites) is sufficient for
bounded prerequisites:

| Condition | Required evidence |
| --- | --- |
| `verified` | Verified task receipt, no unresolved execution issue, enrolled learning accounted as `ready`. Failed/skipped/no-change learning remains labelled accurately. |
| `integrated` | The above plus actual final task/learning commit ancestry in a named local ref, default `refs/heads/main`; use `refs/heads/dev` where appropriate. |
| `approved` | The verified result plus explicit approval bound to that result fingerprint and this dependent assignment. It cannot replace verification or authorize merging. |

Unknown future task IDs wait. Cycles, including forward-reference cycles, are
rejected atomically. Definitions and admission evidence are frozen; changing
unsubmitted work requires explicit deferral and a new task ID. Original legacy
specs/hashes remain unchanged. Dependency changes after submission do not rewrite
the admitted prompt or its proof.

Dependencies do **not** supply a future base or a free worktree. The current
queue reserves one known session/worktree/branch per assignment and refuses a
second queued assignment there. Dependent tasks can run now on independent,
already approved worktrees; same-worktree pipelines need the reuse milestone.
Before the first live edge, use the README's controlled observer handoff and
ledger backup procedure. An already-running old process is not fenced by a
schema-version check on new connections. Do not migrate merely to update a plan.

## Delivery milestones

Each milestone should be a separately reviewable change with disposable Git/API
fixtures. Keep the live Qwen limit at **three including learning**, Big Pickle
serial, production GPU priority, existing reservations and human integration
approval. DeepSeek PoC contexts stay outside this short-task pipeline.
Admission deadlines need drain headroom; they do not guarantee a GPU finish time.

| Milestone | Smallest useful deliverable | Prerequisite |
| --- | --- | --- |
| M1 | Observable, restartable existing runner | Working batch queue |
| M2 | Approved bulk intake and one dependency pilot | M1 |
| M3a | Reviewed synchronization and fresh assignment binding | M1/M2; integration evidence |
| M3b | Declared private-worker/build reservations | M3a before automatic native-worker reuse |
| M4 | One policy-approved clean continuation | M1; existing recovery checks |
| M5 | Consolidated frozen review/integration handoff | M1; existing integration gate |
| M6 | Bounded follow-up drafts | Routine operation stable through M1–M5 |

### M1 — Trustworthy status and runner operation

Extend existing status/reporting with last successful observation, runner
heartbeat/exit, task versus learning stage, named capacity holds, pending forms,
provider/window state and ready-unit receipt references. Use recorded process
identity in its owning execution context; neither a bare PID nor a missing PID
from another context establishes liveness. Make one documented bounded launch
of `batch.run` survive its launcher, using existing process/supervision facilities
where available, and record code/schema identity at launch. Reconcile first on
restart; keep stop-admission separate from
observation and from cancelling agents.

Acceptance: kill/restart the observer at the supported boundary; no duplicate
prompt, lost cursor or interrupted agent. Independent tasks continue around a
held unit. Report stale/unknown observation honestly. Test admission cutoff and
later drain deadline separately. No model call is needed for this milestone.
Implemented as opt-in [runner/status support](README.md#operational-status-and-bounded-observer)
in `runner.py`, `operations.py` and `batch.py`, with dedicated disposable-process
fixtures in `test_runner.py` / `test_operations.py`. No ledger schema change or
live observer takeover is performed. Adoption needs the documented owned handoff;
M2 is the next implementation milestone after that operational check.

### M2 — Import a complete approved backlog

Add a small validated bulk input to the existing enqueue path, not a persistent
`controller.py` plan engine. Each item supplies stable task ID, bounded prompt,
exact output paths, pinned inputs, approved registry preference, learning policy,
ordering and existing task-ID prerequisites. Record approval scope/digest;
preview collisions, unknown prerequisites and blocked work before import.
SQLite remains the operational source of truth. Put the routine-independent
wording in the reviewed shared task template for **future** assignments; submitted
payloads remain unchanged.

Acceptance: import six approved tasks on independent disposable worktrees into
a pool of three; replaying the import adds no tasks or POSTs; changed definitions
are refused. A failed prerequisite blocks its dependents while unrelated work
drains. Legacy queued rows survive controlled dependency activation unchanged.
Routine choices and owned-job waits cause no operator question; genuine approval
or unavailable input produces a precise blocker. Automatic question answering
is unnecessary for this slice.

### M3 — Reuse completed sessions, worktrees and workers safely

Introduce only the missing prospective-assignment intent and cooperative resource
ownership needed to bind a future approved task after its predecessor settles.
Do not enqueue it with a guessed base. Freeze its predecessor's task/learning
receipt before synchronization, record actual integration ancestry, then recheck
session/forms, physical worktree/branch, local artifacts and expected source pins.
Fast-forward only under the approved policy; renew registration at that exact
base. Changed inputs or an unexpected move need review. Reserve an existing
private worker/build root where needed; source-only tasks need no container.
Deliver this in two patches: M3a handles synchronization and registry renewal;
M3b adds declared worker/build reservations before automated native-worker reuse.

Acceptance: two successive approved tasks reuse one persistent context after
accounted learning and reviewed integration. Inject dirty files, an external
writer, branch/placement drift, source-pin change, shared-worker collision and
an interrupted synchronization; each preserves work and holds only its task.
No reset, rebase, evidence cleanup or container provisioning is introduced.
Requires M1/M2 and an additive schema decision reviewed with migration fixtures.

### M4 — Bounded continuation under explicit policy

Reuse recovery planning and approval hashes. A future opt-in policy may prepare
at most one continuation of a proven clean stopped attempt, including an
approved Big Pickle-to-Qwen route when the GPU pool opens. Complete retained
events, settled session tail, no pending forms, unchanged checkpoint and absent
tool effects are required; absence of tool events in incomplete logging is not
proof. Executed/unfinished tools, partial builds, foreign input or drift keep
the effects review gate. Quota reopening remains an explicit availability input.

Acceptance: clean quota failure waits for Qwen and continues once; process death
before/after continuation admission never resends. A tool action that happened
without a terminal event stays uncertain. Preserve the completed correction if
learning fails or quota prevents learning; record the outcome. Do not recursively
learn from learning or silently change a running task's model.

### M5 — Consolidated review and integration handoff

Reuse frozen units, existing merge-tree preflight and integration/review receipts.
Add read-only reconciliation of manual merges from actual task/learning ancestry;
do not recertify historical results from a worktree that has since advanced.
Prepare the proposed combined target diff, source/test identity and each learning
delta together. Routine mechanical checks need no inference. Existing Codex
review defaults to medium; caller-supplied risks raise failed tests, unexpected
diffs, API/ABI or approval issues to high, and ambiguous source correctness,
memory safety or conflicting evidence to xhigh. The current runner does not
infer all those risks automatically. Preserve the nested-Codex refusal and
explicit paid-review budget.

Acceptance: multiple ready units form one useful handoff; a conflict or disputed
result holds only that unit. Target drift invalidates its review. Review success
does not itself approve merging; lost Git/hook outcomes stay uncertain. Test a
paid review once from an approved ordinary shell before calling that path live
validated. A Qwen advisory review fallback is optional later, with explicit
provenance and no automatic acceptance privilege.

### M6 — Optional replenishment proposals

Only after M1–M5 reduce routine supervision, let a bounded Qwen task propose
follow-ups from PLAN and frozen verified evidence. Match known task IDs to avoid
duplicates and prioritize existing correction/release gates. New proposals are
drafts for approval; preapproved checklist steps can flow through M2/M3 directly.
Do not infer authorization from report text, a schema-valid response, path
overlap or an agent's commit. Stop when approved useful work runs out.

Acceptance: duplicate, superseded, blocked and out-of-scope proposals cause no
dispatch; independent approved work continues. Cap planning calls and queue
growth. No automatic prompt rewrite, broader audit or indefinite task generation.

## Rollout and stopping point

Ship M1 first, then M2 and a controlled dependency pilot. Defer M3's schema work
until sequential context reuse is the observed bottleneck. M4 and M5 can then
be delivered independently under their existing approval boundaries. M6 is
optional; a finite well-prepared backlog already delivers useful unattended
work without an autonomous research planner.

Use a pool-sized disposable pilot before enabling each new mutation path. Stop
admission outside the approved resource window, at the finite run budget, or
when no suitable approved task remains; keep observing admitted work to the
separate drain deadline. A scoped blocker must not become a global stop unless
it actually consumes a required resource or exposes an infrastructure failure.
Keep runtime state/reports private; retain technical decisions and evidence links
in Git. This review changes documentation only.
