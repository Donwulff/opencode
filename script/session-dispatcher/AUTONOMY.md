# Unattended operation: current capabilities and delivery milestones

**2026-11-13 implementation update:** the opt-in observer now supports policy-bound
Qwen proposal intake and automatic backlog replenishment (`autonomy.py`), continued
shadow collection with admission fairness, and explicit weekend observation windows.
The [README workflow](README.md#policy-bound-automatic-replenishment) owns the current
contract. These supersede the historical missing-selector/refill and three-disagreement
stop descriptions below only when the new policy is explicitly enabled. Integration,
substantive acceptance and prerequisite approvals remain separate. A settled held task
can release inference capacity while retaining every worktree/evidence lease. No model
judgment grants itself broader execution authority or merges a branch.

Updated 2026-11-12 for the native coordinator throughput controls, the
metered-inference allocation appended the same day, and the adversarial
review corrections plus shadow-review experiment evaluation below, independently
checked against code and live receipts during the completed-work integration. The original
2026-11-10 assessment used dispatcher commit
`d59c11f64eb56f1c9fdf64f858fd0fe1a8585b88`, its tests, the running ledger
(read-only), and the recent Qwen batches. This replaces the initial Qwen design
at `64e8d9e656` and its 2026-11-09 amendment; their full rationale remains in Git.
It is an implementation roadmap, not authorization to change live assignments.

## Operational priority

The objective is completed, verified delivery with an approved backlog ready for
the GPU, not the fewest coordinator calls. Routine choices within the operator's
standing correction/release scope belong to the coordinator; they should not
wait for another human "continue". External inputs, new authorization and
consequential release/policy choices remain operator decisions. Qwen can provide
bounded evidence reconciliation, repair and follow-up proposals without gaining
integration authority. Routine Codex coordination remains medium effort; model
tiering is an optimization after a dependable flow, not a prerequisite for it.

The concrete bottlenecks observed on November 12 were missing queue-refill
notifications, a ten-minute review cooldown on short worker tasks, and a single
escalation already spent when unrelated issues arrived. Opt-in queue-pressure
wakes, urgent prerequisite reviews and bounded per-issue escalation now address
those in `coordinator.py`. A stable workset does not create a planning loop.
All existing task/learning verification, ownership, resource and merge gates stay.

The next useful acceptance test is the live approved delivery graph draining
through learning, review and dependency admission while new useful work is
prepared. Do not postpone that behind a bulk importer or a new scheduler.

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
units and named blockers. Integration keeps its existing review and approval
boundaries; standing operator authorization can cover Codex-reviewed safe merges.

## What works, and what still needs work

| Capability | Current implementation and evidence | Remaining boundary |
| --- | --- | --- |
| Persistent sessions and source identity | `dispatch.py`: explicit registry pins session, model, agent, canonical worktree/branch/base and retained event cursor. | No automatic discovery/allocation or registry renewal. A clean checkout and native directory metadata alone do not establish ownership; manually moved sessions have made those disagree. |
| Queued delivery and observation | `batch.py`: deep frozen backlog, stable task/message IDs, intent before one POST, durable replay, Git verification and assignment-scoped error handling. Recent live batches use it. | Admission deduplication does not prove tool effects happened once. Observer restart reconciles existing intent; it does not resend uncertain work. |
| Resources and hours | Provider pools enforce capacity, external occupancy, finite admission deadline, quota/rate-limit pauses; learning shares capacity. | No recurring calendar, production-GPU detector, cross-host/provider-alias accounting or enforced container/build leases. Those remain explicit coordinator inputs. |
| Post-task learning | `lifecycle.py`: once-only disposable fork, Qwen override, existing learn prompt snapshot, optional separate commit, failure/no-change accounting and archival. Live use plus native fixture smoke. | The fork shares the task's filesystem. Keep the worktree reserved through accounting. Pre-admission wait/skip is bounded; an uncertain executing fork cannot simply time out and release ownership. |
| Dependencies | `dependencies.py`: immutable task-ID edges, `verified` / `integrated` / `approved`, admission proofs, cycle checks and scoped holds. Schema 6 was activated for the November 12 approved delivery graph after backing up the stopped ledger. | Native build jobs await reviewed integration of their exact prerequisites; live dependency admission is distinct from fixture coverage. |
| Interrupted-task continuation | `recovery.py`: checkpoint/effect evidence, approval hash, one durable continuation intent, deliberate model handoff and capacity waiting. Big Pickle quota and Qwen continuation have live operating evidence. | Planning/preparation and quota reopening need explicit approval. No standing retry policy or unattended effects judgment exists. |
| Review and integration | `review.py` and `integrate.py`: frozen unit/target, merge-tree preflight, structured bounded review, explicit approval and history-preserving merge receipt. | Fixtures cover the mechanics; the live record has 20 deferred integration enrollments because coordination used manual reviewed merges. This is not 20 outstanding unmerged results. No paid end-to-end review is claimed by the fixture pass. |
| Coordinator throughput | Native Codex wake-ups for ready units, issues and opt-in independent-backlog shortfalls; urgent dependency reviews; durable per-issue escalation within explicit budgets. | The coordinator chooses useful work and performs existing safe integration/renewal commands. The notifier does not select technical work, grant authorization or retry uncertain effects. |
| Prompts and feedback | Versioned task/learn/continue/review templates; stored hashes and exact submitted payloads; event-linked feedback. Latest task assignments explicitly avoid routine questions. | That instruction currently lives in batch text, not a shared template. No automatic question responder or prompt rewriting exists. |

Original November 10 version observations: Python 3.14.4, Git 2.53.0, Bun 1.4.4, installed OpenCode CLI
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
fixtures. Preserve the operator-configured Qwen limit **including learning**
(currently four), Big Pickle serial, production GPU priority, existing reservations and human integration
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

### M2 — Import convenience (not a throughput prerequisite)

The existing enqueue API already populated a seven-task dependency graph in
live use. A file importer reduces setup work but need not precede automatic
admission, coordinator-driven refill or useful sequential work.

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

The read-only `status --review-ready` handoff and bounded `coordinator.py`
notifier implement part of M5: batches can wake one approved persistent Codex
coordinator through its native queue. Dedicated-context resume and effort control
are explicit opt-ins with finite total wake and per-issue escalation budgets.
Backlog pressure can trigger replenishment without waiting for another result;
ready prerequisites use the configured urgent cooldown. Source correctness
and safe integration still use existing gates. This does not implement automatic
new-investigation selection or M3 worker reuse. See the README for exact controls
and the native transport/ownership limits.

### M6 — Replenishment within standing scope

The existing Codex coordinator may select and approve bounded follow-ups within
the operator's standing delivery scope now, using PLAN and frozen verified
evidence. It can ask Qwen for proposals or routine repair where useful. Match
known task IDs to avoid duplicates and prioritize correction/release gates.
Work outside that scope remains a draft for approval; preapproved checklist
steps can flow through the existing enqueue/dependency path directly.
Do not infer authorization from report text, a schema-valid response, path
overlap or an agent's commit. Stop when approved useful work runs out.

Acceptance: duplicate, superseded, blocked and out-of-scope proposals cause no
dispatch; independent approved work continues. Cap planning calls and queue
growth. No automatic prompt rewrite, broader audit or indefinite task generation.

## Metered-inference allocation and next work (2026-11-12)

Corrected in place after the adversarial review below; the "Expected
effect" paragraph and the routine-review row of the classification table
are retracted or requalified where they failed scrutiny.

The 2026-11-12 coordinator window spent all fifteen wakes
(`ledger.sqlite3.coordinator.json`, `max_wakes: 15`): twelve medium turns and
three xhigh escalations. The escalations were acceptance-level judgments
(MP08 unit memory safety; MP10 context-port memory safety; smoke-maps source
ambiguity where an independent counterexample to the candidate was found).
The medium turns reconciled state, drafted and enqueued follow-up Qwen tasks
(`coordinator-20261112/backlog-23890/`, `followup-65b9df79/`), drafted
dependent and repair tasks (`mp10-review-6f8c/`, `maps-repair-ec6e/`,
`smoke-payload-review-c8d8/`), and ran the safe integration/synchronization/
renewal commands named in the template. Each turn's effects need evidence
reconciliation; this may be performed by
the authorized coordinator and does not require a new operator response. Metered
turns remain a scarce coordination resource.

**Provable-difference test.** Assign an activity to metered inference only
when it is (a) an acceptance-level judgment, not labor, (b) about source
correctness, memory safety, conflicting evidence or scope, not bookkeeping,
and (c) consequential if wrong and undetectable by the deterministic gates
downstream (verification, merge-tree preflight, ancestry receipts, operator
approval). Everything else moves to deterministic code or local Qwen. Qwen's
marginal cost is about zero — one stream and a fully loaded pool cost the
same — so while useful work exists *and a pool window is open* the GPU should
be running Qwen streams, and Qwen work should fill the slots during metered
cooldowns. (Windows are explicit operator inputs; e.g. `qwen-dedicated`
closes 2026-11-13T05:15Z and quota re-enablement is explicit by rule, so
this bound does not hold across window boundaries.)

### Observed-day classification

| Activity on 2026-11-12 | Class | Destination |
| --- | --- | --- |
| Three xhigh escalations (memory safety x2; source ambiguity with counterexample) | Judgment; independence is the point | Keep metered; per-issue budget unchanged |
| State reconciliation inside each turn | Git/ledger/native metadata is already assembled by `operations.report()`; source correctness and result acceptance are not | Consume the deterministic snapshot; retain fresh ownership/Git checks before mutation and inspect substantive evidence |
| Drafting/enqueueing two-to-three follow-ups per wake (refill, dependent, repair tasks) | Labor: reference lookup, deduplication, bounded prompt writing | Qwen drafting (free); deterministic dedup/eligibility; batched approval |
| Safe integration/synchronization/renewal commands | Mechanical under verifiable preconditions | Deterministic M3a commands under existing approval receipts |
| Reviewing routine units (all five unmerged units are `completed_no_changes`/`completed`) | **Corrected:** "no risk flags" was vacuous — none of the five had a formal integration/review row at this snapshot, and risk flags are caller-supplied at enrollment (`integrate.py:60-61`), not a discovered property of delivery. A missing formal row does not establish absence of manual Codex review. Deterministic gates detect contract-shape errors only; these units are content-bearing (evidence, arithmetic, assembled kits), and content errors pass every deterministic check. | Keep the existing content-review and approval gate; shadow agreement alone cannot authorize replacing it; reviewer must be a fresh session, never a fork of the author session (`lifecycle.py:131-132` forks the author) |

### Next work, in order

1. **Consolidate and clear the genuinely unmerged units.** The read-only
   handoff against `main` (`batch.py status --review-ready --repository
   <repo> --ref main`) counts 119 of the 132 ready units
   `already_integrated` — an ancestry fact, **not** a current-content
   claim: 51 of the 119 have their own delivery paths modified in main
   since the unit's final commit (35 AGENTS.md-only learning churn; 16
   substantive, e.g. later correction tasks replacing an earlier unit's
   `httputil.py`/artifact hashes). Five units are `awaiting_review`
   (mp09-inttest4-rpm, smoke-v3-payload-gate, mp09-oracle-plan-review,
   smoke-v3-payload-offline-review, smoke-v4-assembly) and eight are
   historical `needs_review` rows (manual/accounted; retirement cannot
   clear them). `qwen-ol6-mp09-inttest4-rpm` (final `3e0858ae…`, not in
   main) is the only unit gating the two `integrated`-dependent pending
   tasks (mp0910-combined-rpm, inttest4-partial-kit; the other
   prerequisites mp10-context-port and smoke-descendant-fix are already in
   main). **Corrected:** "through the existing gate" overstates
   determinism — `integrate.decision` (`integrate.py:155-162`) accepts only
   a `review` table row produced by the Codex-only `review.run`, so a
   routine unit reaches the gate either via one metered review or via the
   authorized coordinator's or operator's reviewed manual merge (the practice behind the 119). The
   *dependency* unblocks on ancestry alone (`dependencies.py:110-124`
   checks `final_commit` against `refs/heads/main`): any merge path that
   puts `3e0858ae…` in main satisfies their ancestry requirement. Admission
   still needs the other existing checks; technical acceptance is not proved.
   The combined-build prompt explicitly requires usable accepted source/tests
   and a blocker checkpoint otherwise. This integration found an accepted
   fixture failure plus conflicting unexecuted oracle predictions, so record
   that qualification before admitting successors. The other four units are
   independent. These counts describe the pre-integration snapshot, not a live board.
2. **Read-only merge-tree preflight in the handoff.** Extract the existing
   `integrate.freeze` preflight without enrollment: proposed merge delta,
   conflict/drift and frozen target. It creates Git objects, so describe it as
   ref/worktree-preserving rather than filesystem-read-only. This reduces
   repeated preparation; it does not replace content review.
3. **M3a synchronization and renewal.** Automate the repeated safe commands
   under existing scope, ownership, artifact and approval checks. This is the
   first substantial implementation priority: it enables useful same-context
   delivery pipelines, whereas hiding historical rows does not free workers.
4. **M6 bounded refill drafts.** Let fresh Qwen sessions prepare useful
   report-only follow-ups from PLAN and frozen results. Deterministic checks
   deduplicate and assess eligibility; the authorized coordinator gives one
   concise batch approval. Preapproved checklist steps can use existing enqueue
   directly. Keep planning caps and do not manufacture research to occupy slots.
5. **Optional historical-row view.** `operations.review_ready` already counts
   and omits integrated receipts (`continue` after `already_integrated`), so
   the 119 rows do **not** bloat the notifier's unit handoff. The pre-integration
   handoff contains five awaiting units plus eight manual/accounted rows.
   A proposed `retire --integrated-at <sha>` could simplify the full status
   display, but is unimplemented and lower priority. It must preserve historical
   ancestry and audit state, not assert current content or release reservations.
6. **Blocked-row accounting.** The old OL6 inttest3 row retains two untracked
   `tests/mp08-probe.*` files and an unresolved verification issue. Its HEAD
   `6913176` and cited `8b92a823…` being reachable in main do not produce the
   missing verified task/learning receipt or authorize clearing the hold.
   Classify/preserve the files and reconcile via existing explicit accounting;
   do not treat retirement of ready rows as a solution for this blocked row.
   The current window spent 15/15 wakes; a new finite window must be deliberate,
   preserving old receipts rather than resetting its budget.

### What stays metered, and why

- Acceptance review of units with non-routine risk flags: independence from
  the Qwen family that produced the work is the provable difference, and the
  smoke-maps counterexample is the observed payoff (provenance verified:
  the counterexample commit `a3d71c6` postdates the Qwen task's
  verification and its learning commit by minutes, sits on a path matching
  no ledger task ID, and is outside learning scope — it was committed by
  the coordinator turn, 51 s before the escalation).
- Budgeted per-issue escalations (unchanged).
- Any Qwen reviewer (advisory or shadow) must be a **fresh session**
  consuming only the frozen evidence, never a fork of the author session —
  the dispatcher's learning fork machinery (`lifecycle.py:131-132`) forks
  the author and inherits its context, which would make the "independent"
  review a self-review.
- Batch approval of follow-up candidates in unattended windows: bounded
  short input; M6's "do not infer authorization from report text" rule
  unchanged; nothing auto-dispatches.
- A Qwen advisory receipt never satisfies `integrate.approve` alone: the
  confirmed-routine plus explicit risk-acknowledgement flow and human
  approval are unchanged, so quality is gated by the existing receipts
  regardless of which model wrote the receipt.

**Expected effect — retracted as stated.** The "fifteen to four-to-six
turns, wake budget two-to-three times longer" projection failed the
adversarial review: every Qwen-tier task that delivers a commit becomes a
verified task + ready unit, and `coordinator.collect()`
(`coordinator.py:187,195`) turns every such unit into a `unit:` wake key.
The Qwen tier therefore *feeds* the wake stream at its own output rate
(today: ~15 verifications per 14 h; 30 seen keys across 15 wakes), and the
132-row backlog is the empirical accumulation pattern (119 units needed
manual merging; none were ever retired — no `retire` command exists). The
projection holds only if Qwen-tier outputs use a non-unit receipt channel
(ledger rows or run-dir files, not task deliveries) — which this plan does
not yet define. The projection also rests on one observed window. What
survives: metered turns concentrate on judgment work, and Qwen streams fill
slots that metered labor no longer occupies *inside open pool windows*.

## Adversarial review of the allocation plan (2026-11-12)

A same-day adversarial pass attempted to disprove each claim of the
allocation section using code, ledger state and Git provenance (read-only).
Verdicts, with the evidence that decided each:

| Claim | Verdict | Deciding evidence |
| --- | --- | --- |
| Integration bottleneck: one unit gates both pending tasks | **Survived** four disproof attempts | `queue_dependency` edges all target `refs/heads/main`; both pending worktrees clean and exactly on-base (0 commits ahead, 0 dirt); registry snapshots match specs; `batch.py:266-290` admits on tick once `dependencies.states()` is satisfied; only `3e0858ae…` is absent from main. Fragilities (not refutations): shared worktrees (19-23 other ready rows each) can stale the base; pool window closes 2026-11-13T05:15Z |
| 119/132 "already integrated" | **Survived as historical fact, refuted as current-content claim** | Independent re-scan: 119/132 ancestry-verified, but 51/119 have their own paths modified in main since (35 AGENTS.md-only; 16 substantive). Pinned fixture `test_ready_receipt_is_historical_without_a_fresh_git_claim` already encodes the historical semantic |
| Smoke-maps counterexample = Codex payoff | **Survived** | Qwen task verified 15:22:21Z; learning commit 15:24:38Z; counterexample `a3d71c6` at 15:28:13Z (51 s before the 15:29:04Z escalation), on a path matching no task ID, outside learning scope |
| Risk classification supports the routine/non-routine tier | **Refuted** | Flags are caller-supplied at enrollment (`integrate.py:60-61`, CLI `--risk`); only auto-flag is `unexpected_diff` (`review.py:43-44`); the tier key is an absence of caller-supplied labels, not a semantic correctness check. Contract-shape errors are deterministic-detectable; content errors (arithmetic, patch semantics) are not, and this project's units are content-bearing. A routine-enrolled unit with a semantic defect passes every deterministic gate and a same-family advisory receipt |
| Qwen advisory review is "independent" | **Refuted as specified** | Unspecified reviewer provenance; the only fork machinery forks the author session (`lifecycle.py:131-132`, provenance-pinned), which inherits the author's context — structurally the weakest reviewer. Fresh-session requirement now added above |
| Gate offers a non-metered path for routine units | **Refuted** | `integrate.decision` (`integrate.py:155-162`) requires a `review` row from the Codex-only `review.run` (`review.py:62`, `shutil.which("codex")`); no human-review path exists; advisory receipts are explicitly denied acceptance privilege. The gate's review leg is metered by construction |
| Wake savings "15 to 4-6 turns, budget 2-3x" | **Not established** | Qwen-tier deliveries create `unit:` wake keys (`coordinator.py:187,195`); at today's rate (~1/56 min) a standing pipeline keeps the wake stream saturated and the 15-wake budget is consumed by batch approvals of Qwen output; 132 ready rows are historical deliveries, not 132 pending reviews; no controlled comparison establishes a savings factor from this one window |
| Unattended operation | **Qualified** | Every unattended night needs a fresh coordinator window (explicit deadline; current window spent 15/15, deadline 2026-11-13T06:15Z), pool re-windowing (explicit by rule), and an approval pass (human or metered) the section omitted |

Operational note from the review: Git log dates display in local time
(UTC+2) while the ledger stores UTC. A first read made the counterexample
commit appear to postdate its escalation by three hours; normalized, the
ledger and Git agree to the second (queue `updated 16:00:02.14Z` == commit
`18:00:02` local). Any tool correlating the two sources must normalize, or
it will manufacture false integrity alarms.

Claims that also survived: the coordinator mechanism (31 fixtures in
`test_coordinator.py` pin budget/batching/dedup/escalation fencing —
verified properties, not intentions); the blocked-row characterization
(dirt = exactly two untracked MP08 probe files; HEAD `6913176` in main;
cited commit `8b92a823…` reachable).

## Shadow-review calibration experiment

**Operator-authorized bounded trial, 2026-11-12:** `shadow.py` and the opt-in
coordinator hook now implement forward capture plus labelled cold replay using
the existing queue and learning. Fresh report-only sessions have frozen local
inputs and no author-session context; references are recorded before exposing
shadow results in the handoff. Ten units or three classification disagreements
stop further case creation, pending adjudication. Existing delivery controls stay.
This is a unit-level trial, not a statistical error-rate estimate or acceptance
policy change. [README commands and recovery limits](README.md#bounded-shadow-review-trial)
own the implemented contract; the design discussion below is its rationale.

Purpose: convert the two refuted premises (risk-tier safety, same-family
review independence) from argument into measurement. Fresh, independent
Qwen sessions review the same frozen evidence and decisions currently
handled by metered turns; judgments, material disagreements, missed errors
and costs are compared. No additional Codex calls are made in either mode.

### Corpus facts (decide feasibility)

- The live ledger has **no `review` table**: zero saved formal `review.run`
  reviews are present (reviewed manual merges were used; M5
  already warns "No paid end-to-end review is claimed"). A bounded-review
  replay corpus therefore **does not exist**.
- What does exist and is recoverable: the 15 stored wake handoffs
  (`coordinator-20261112/handoff-*.json`, exact wake inputs), the 3
  escalation requests (risk/reason/issue JSON), the working dirs holding
  routine review/repair briefs (`mp10-review-6f8c/`,
  `smoke-payload-review-c8d8/`, `followup-65b9df79/`, …), and the actual
  outcomes (ledger + Git). Pinned main HEADs at each escalation time are
  recoverable (`git rev-list -1 --before=<t> main` → `8fbcc54`, `15ea469`,
  `a3d71c6`), so disposable cold-replay worktrees are possible.
- Coordinator *turn* decisions are only partially recoverable: effects
  (commits, enqueues, sync receipts) are in Git/ledger; the thread's
  reasoning is not, and the thread's accumulated context cannot be
  reconstructed. Bounded `review.run` reviews, by contrast, are fully
  replayable (the saved plan carries `unit_hash`/`evidence_hash`/
  `integration_hash`, so a recomputed frozen assignment is
  hash-verifiable before use) — but there are none to replay yet.

### Design (two modes, both zero additional Codex calls)

1. **Seed cold replay (offline, starts with what exists).** For the 3
   escalation cases plus 2-3 recoverable routine verifications: pin a
   disposable worktree at the then-main HEAD; give a **fresh Qwen session**
   (not a fork) only the frozen input (handoff/escalation brief, unit
   receipts, diffs) with the same untrusted-data framing as
   `templates/review.txt`; require a bounded structured judgment
   (integrate/hold/escalate + named evidence). Compare with the
   recoverable actual outcome; adjudicate every disagreement with the
   operator (ground truth exists for these cases: what happened next, e.g.
   maps held-then-amended `61ef7c4`, MP10 integrated, MP08 kept on hold).
2. **Forward shadow (additive, from the next coordinator window).** For
   each metered wake, enqueue a Qwen shadow task carrying the *same*
   handoff JSON as a fresh session, producing the same bounded judgment
   without seeing the metered turn's actions; join against actual effects
   after the fact. Shadow tasks run on idle pool slots only — never
   displacing delivery work, and never counted against `backlog_target`.

### Metrics, and what each one can and cannot prove

- **Classification agreement** (integrate/hold/escalate/draft): measures
  Qwen-vs-Codex divergence on real decisions. Agreement is *not* joint
  correctness — correlated failure modes (shared training, same plausibility
  biases, same arithmetic weaknesses) make "both agree" compatible with
  "both wrong". It is a lower bound on nothing; it is a triage signal.
- **Material disagreements**: the interesting output. Each becomes an
  adjudicated case (operator or later event decides who was right). This is
  the only part that accumulates ground truth, and it will be slow.
- **Missed errors**: cannot be measured at this sample size. The one
  known caught-defect case (smoke-maps) calibrates nothing statistically.
  Planted-defect fixtures can test attention/format reliability separately,
  but synthetic misses do not estimate real-world miss rate.
- **Cost**: Qwen wall-clock per shadow (marginal $0, but real GPU minutes);
  Codex metered cost per shadowed turn, for ROI. Record model IDs/versions
  per case — the calibration decays on model updates.

### Limitations, stated up front

- **Selection bias, in the unhelpful direction.** The recoverable corpus is
  exactly what metered turns did: hard escalations and safe
  integration/review labor. The population the tiering actually wants to
  offload — routine, unflagged unit reviews — has *no metered reference to
  shadow*, because there are no saved formal `review.run` results. Manual Codex coordinator
  reviews do occur, including routine units, but their frozen input, decisions
  and full accumulated context are not all represented in a bounded-review plan. Shadow
  calibration therefore cannot, by itself, authorize moving routine units
  to Qwen; it can only bound the judgment-tier question. Seeding a routine
  reference corpus would require deliberately metered reviews of routine
  units — a small, explicit cost, an operator decision.
- **Cold-vs-warm confound, both directions.** A fresh Qwen session lacks
  the coordinator thread's accumulated context, so a shadow "loss" may be
  a context deficit, not a model deficit; a shadow "win" on frozen inputs
  may not transfer to a warm thread. Report both and do not collapse them.
- **Same-family correlation.** The shadow is Qwen reviewing Qwen-produced
  work: independence is from the *session*, not the family. A negative
  result (Qwen misses what Codex caught) does not prove Codex uniquely
  correct — only that cross-family review added value in those cases.
- **Power.** At observed review rates the corpus grows by single digits per
  week; expect directional evidence and a handful of adjudicated cases, not
  error-rate estimates, for months.
- **Scope discipline.** This is calibration, not a subsystem: run dirs,
  receipt files, one report script. No new tables beyond a shadow-receipt
  store if one is even needed; removable when the decision is made.

### Stopping rule and verdict

Stop after **10 shadowed turns or 3 material disagreements, whichever
first**, then write a one-page decision memo (tiering evidence, not
authorization) and dismantle the shadow path.

**Verdict.** Seed cold replay is an optional later experiment (local,
disposable worktrees, frozen inputs, zero additional model calls to Codex). It
tests the two refuted premises on this project's actual decisions, and its
adjudicated cases may be useful, but task setup, GPU time and disagreement
adjudication are real costs. Prioritize deliverables and M3a first; this review
does not authorize or start the experiment. Do **not** gate the
tiering rollout on the experiment — the human approval and existing gates
stand meanwhile, and the experiment's population bias means it cannot
authorize the routine-tier move on its own. Do **not** build the forward
shadow as standing infrastructure before the seed replay justifies it; the
current integration loop (manual merges) is the de facto reference, and
forward shadowing only becomes valuable once metered reviews are actually
happening at a rate worth calibrating.

## Rollout and stopping point

M1, the approved queue/dependencies and native coordinator are operating. First
trial the throughput controls on the current delivery graph. Next automate the
repeated synchronization/renewal commands of M3a if they remain the bottleneck;
current safe manual commands are already available to the coordinator. M3b/M4
need their own bounded resource/recovery trials. Bulk intake and cheaper-model
routing can follow measured need. No autonomous research planner is required.

Use a pool-sized disposable pilot before enabling each new mutation path. Stop
admission outside the approved resource window, at the finite run budget, or
when no suitable approved task remains; keep observing admitted work to the
separate drain deadline. A scoped blocker must not become a global stop unless
it actually consumes a required resource or exposes an infrastructure failure.
Keep runtime state/reports private; retain technical decisions and evidence links
in Git. The notifier changes scheduling notifications only; it still cannot
approve a correction, admit Qwen work, merge branches or create containers.
