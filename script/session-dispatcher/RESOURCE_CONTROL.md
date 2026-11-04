# Resource admission for the manual dispatcher

Implementation note (2026-11-04): [the learning lifecycle](LIFECYCLE.md) implements
only a cooperative GPU/provider admission gate for future enrolled learning forks
(maximum three), not the general scheduler proposed below. The dated operating
observations in this design are historical; current operator availability controls
new admissions. Codex now has an explicit bounded review command, not a task scheduler.

The remaining sections are the original design, based on reported working
limits and throughput/KV-cache clarification on 2026-11-02. General resource
pools, task queues and automatic dispatch remain unimplemented; the learning
gate above is the bounded exception. The [task CLI](README.md) remains explicit.
Do not resubmit the assignments already handed to the investigation agents.

## Starting policy

Treat these as configurable operating defaults, not measured hardware limits.
The GPU being free now is an availability observation, not permanent capacity.

| Resource pool | Normal active assignments | Ready locally | Gate |
| --- | ---: | ---: | --- |
| Qwen3.8-27B-FP8 / shared GPU | 3 | 1 | Operator enables availability; account for other GPU users before admission. |
| DeepSeek CPU / UD-IQ2_XXS | 2 latency-oriented; up to 4 in a configured throughput profile | 2 at the lower concurrency | Normally available; preserve the existing 128k context configuration. Exact utilization tuning is deferred. |
| OpenCode Big Pickle | 0 while quota-exhausted; 1 provisional slot only after operator re-enables it | 0 newly admitted now | Operator reports free quota exhausted. Unknown/exhausted blocks admission; no quota probing by inference. |
| Codex | Manual for now | Operator's list | Commercial budget/availability is operator-controlled; the V2 dispatcher has no verified Codex dispatch or quota adapter. |

The operator reports higher combined throughput with more batched work, at the
cost of much slower individual results. Non-interactive work may deliberately
use a higher configured concurrency; this need not be a one-off exception.
Keep latency-oriented and throughput-oriented settings separate, and select
the profile explicitly rather than inferring it from queue length. No limits
are changed by this document.

For Qwen, the reported current KV allocation fits approximately **two full
128k contexts**. Three active assignments usually work well because they are
rarely full together; the fourth waits locally until a slot and context/memory
budget permit admission. This is an operator observation, not a measured hard
256k-token admission guarantee: sharing, overhead and actual live context sizes
still matter. Do not promote the waiting fourth automatically just because
non-interactive latency is acceptable. Higher concurrency remains possible
only when its memory requirements are supported.
Big Pickle's proposed single slot is a conservative starting setting, not an
observed provider concurrency limit.

The operator reports that Big Pickle's cap/POD task has been handed to Qwen in
the same persistent OpenCode session. Current session model and existing effects
must be reconciled; do not create a second assignment because the provider
changed. The manual CLI's model-binding drift check remains intact: it does not
silently authorize a mid-assignment takeover. Any future handoff support needs
explicit operator approval and resource-accounting reconciliation.

Here a slot means an admitted OpenCode execution, including time spent using
tools. It does not mean a continuously decoding inference stream. Hold a slot
for the whole execution initially; this may underuse the backend during tools,
but can be implemented with the verified session API. Backend batching remains
the inference server's responsibility. Qwen capacity is gated by GPU use even
when a slot count permits admission. Long contexts can reduce safe concurrency;
do not treat three slots as a memory guarantee or truncate context silently.

Initially use a manual GPU available/busy gate, optionally supplemented by
operator-defined wall-clock availability windows. The operator currently watches
for other GPU work; detailed utilization measurement and exact scheduling are
deferred. Elapsed time alone is not proof of free GPU memory or a completed
task. A window closing pauses new admissions without cancelling active work;
assignment-level admission cannot guarantee an exact GPU handoff time.
The operator expects an imminent production GPU reservation. Do not dispatch
additional GPU assignments or assume an exact completion window from that notice;
the operator is allowing existing work to continue. No cancellation is authorized.

## Three separate reservations

1. **Session/worktree/branch:** keep the current exclusive assignment reservation
   until explicit Git verification. A completed result awaiting human review
   does not make that same checkout available for another assignment.
2. **Inference pool:** reserve before sending the prompt, and release after
   attributable terminal execution plus a fresh idle/no-pending-input snapshot.
   Successful completion can release inference capacity while Git review waits.
   Failure/interruption or missing admission/outcome evidence stays held until
   explicit reconciliation establishes settlement; a timeout/disconnect never
   frees capacity or triggers a resend. Uncertain effects remain uncertain even
   if the operator can separately establish that inference has stopped.
3. **Declared tool resources:** reserve build capacity, exact private build root,
   and any shared mutable target needed by the assignment. Different isolated
   roots may coexist when the build budget permits; two writers to one root may
   not. Read-only access need not monopolize a container. Shared diagnostic
   baseline writes remain forbidden, regardless of free capacity. Model slots
   alone do not prevent concurrent build/tool collisions.

Resource declarations initially govern whole assignments; the dispatcher cannot
enforce tool-by-tool access inside arbitrary agent commands. Do not claim that a
ledger reservation fences humans, other services, provider clients or agents
that ignore the declaration. Provisioning/build authorization remains separate.

## Reuse the existing API and ledger

Keep one SQLite ledger (with a versioned dispatcher-only migration) and the
current stable task/message IDs, approved
registry, atomic reservation, event cursor/hash reconciliation, no-resend rule
and Git verifier. Add only these small ledger concepts:

- A resource-pool record: explicit provider/model bindings, normal/temporary
  concurrency, enabled/paused/unknown state, operator availability observation
  and optional cooldown deadline. Aliases on one GPU map to the same pool.
- An operator-approved pending assignment: stable task ID, registry entry,
  exact task text/allowed paths, requested resource keys and human-defined
  order/dependencies. It has not been sent to OpenCode. Dependencies name
  approved result commits or explicit human acceptance, not assistant prose.
- A reservation record linking an assignment to its pools/tool resources,
  with held/released state and release evidence. Record externally occupied
  capacity separately so manual and dispatched work can coexist.

Do not add another logger, native-database writer, inference client or model
scheduler. Reuse durable session events for outcomes. Task definitions are
private ledger data; do not copy transcripts, credentials or raw error bodies
into status reports or Git.

An initial `plan-next` command should be read-only and make no POSTs. It would
show the first human-ordered eligible assignment, occupied/free slots and every
blocking reason. This is a proposed command, not an existing CLI option.
It must not reserve slots, rewrite the registry or advance saved event cursors;
refresh observations through explicit reconciliation before planning.
Explicit dispatch would then, in one `BEGIN IMMEDIATE` transaction, recheck
queue identity and session/worktree/branch/resource reservations, reserve
capacity and persist the exact request before the sole prompt POST. Recheck
live state and worktree identity immediately before sending. If the process
dies or admission is uncertain, leave the reservations held and reconcile.

Ready tasks stay in the external ledger. `delivery:"queue", resume:true`
can wake execution immediately; it is not a global capacity gate. Pre-admitting
with `resume:false` also is not a reservation against another wake/drain.
Send only after capacity is reserved. A future explicitly enabled runner may
drain this preapproved list, without inventing tasks or changing models. Human
approval remains required for integration; no automatic merge or uncertain retry.

## Account for the work already running

Require an explicitly approved **persistent session**, not merely an existing
Git worktree. Keep these status dimensions separate in the registry/planner:
context present/absent, operator-approved/retired, archived/unarchived,
root/child, active/idle/unknown, and current model/provider. Also check current
placement and worktree collision state. A branch left for evidence or a Codex
checkout without an OpenCode session is not an available independent context.
Do not fabricate one, choose an unrelated older session sharing a directory,
or switch providers just to fill capacity.

The native session list accepts `archived=false` and `parentID=null`; omitting
the archive filter includes archived sessions. Those filters establish neither
operator approval nor current inference health. In this fork the internal
SQLite `time_suspended` value is an execution-recovery claim, not retirement;
do not turn it into a scheduler eligibility flag. The local
[store implementation](../../packages/core/src/session/store.ts) and
[session API schema](../../packages/schema/src/session.ts) own these meanings.
If live status cannot be read, report unknown instead of inferring availability
from an old message or a clean Git status. The existing CLI deliberately has
no worktree-to-session auto-discovery; the future planner must preserve that
explicit-approval boundary.

Start with observation only. Register the manually assigned sessions as
externally occupied and retain their assignment descriptions without sending
anything. Do not fabricate original message IDs, commit trailers or proof of
idempotency for those prompts. Reconcile their actual commits separately;
switch to ledger-dispatched assignments only after the current work settles
and the session/worktree is reviewed and reapproved.

Count the union of known reservations and observed active sessions per pool,
not their sum: the same session must not consume two slots. A saved reservation
also counts before execution appears in the active list. Keep an uncertain
reservation even when it disappears from a snapshot. Include unmapped/manual
sessions and operator-declared other users. Unknown model placement or an
unreachable server blocks new admissions to affected pools until reconciled.

[`/api/session/active`](../../packages/protocol/src/groups/session.ts) covers
foreground drains owned by one OpenCode process, not all inference requests,
other servers, background tools or other GPU consumers. Provider configuration
from [`/api/provider`](../../packages/protocol/src/groups/provider.ts) does not
establish remaining free/commercial quota. Use explicit operator gates initially;
read-only hardware/backend observations can be added later without pretending
utilization alone proves spare memory or capacity. Pausing admission never
interrupts, cancels or moves already running tasks.

## CPU prefill and quota limits

Two DeepSeek executions are a latency-oriented starting point, with two waiting
assignments. A throughput-oriented profile can admit up to four when results
can wait, accepting a slower pace per stream. The operator has tolerated a
15-hour CPU run; long wall time alone must not mark a task failed, free a
reservation or cause a retry. Task deadlines, if any, are explicit assignment
metadata and are separate from short transport/observer timeouts. Preserve the
128k context limit. Stagger large-context starts where practical, without
promising that this eliminates prefill stalls.
The present API does not provide a verified control to serialize each native
provider prefill. Staggering task admission alone cannot prevent later prefills
from overlapping or stalling decode. Do not implement a fake prefill semaphore
from generic session activity. Native [step events](../../packages/schema/src/session-event.ts)
can report request start and response-body completion; these do not separately
measure backend queueing, prefill and decoding. Such control needs verified
backend capabilities or a separately reviewed per-request integration.

For Big Pickle, retain explicit quota state and stop further admissions after
a quota/rate-limit failure. The existing [structured error](../../packages/schema/src/session-error.ts)
can carry `status` and `retryAfterMs` when available. A 429 alone does not
distinguish transient rate limiting from exhausted quota. A cooldown expiry
does not prove restored quota: require an availability refresh, and never
replay the failed assignment automatically. Apply the same budget principle to
Codex manually; no silent fallback to a paid provider or another model.

## Smallest useful next step

Implement only ledger-backed resource declarations, observation-only external
reservations and read-only `plan-next`, with the table above as editable policy.
Test with disposable fixtures: three GPU admissions plus one waiting; two CPU
plus two waiting; manual/dispatched deduplication; concurrent slot reservation;
GPU unavailable/unknown; exhausted/unknown quota; interrupted observation;
uncertain send; and separate inference release versus Git/worktree release.
Use fixture data for occupancy, not active investigations or quota-consuming
probe calls. Then review one proposed admission before enabling a queue runner.
