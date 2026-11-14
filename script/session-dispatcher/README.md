# OpenCode V2 dispatcher

A Python standard-library CLI for one explicitly assigned task at a time per
approved persistent session, physical worktree, and Git branch. It uses the
customized V2 API and native durable session log. The opt-in policy-bound refill
below admits Qwen follow-up proposals; it adds no plugin, inference client or core changes.
An opt-in [post-task lifecycle](LIFECYCLE.md) now accounts for learning forks and
reviewable integration units; `review.py` can invoke one bounded Codex CLI review.
The [opt-in integration gate](LIFECYCLE.md#approved-integration-gate) freezes the
target too, retains reservations, and merges only after explicit approval.

`dispatch.py` owns a **separate private SQLite ledger**, not OpenCode's database.
Use one shared ledger for all cooperating operators. Different ledgers cannot
prevent each other's assignments. Registration is explicit: this tool never
discovers, starts, replaces, or interrupts a managed service or live session.

The opt-in bounded queue below drains approved assignments through the same
ledger and lifecycle, with provider capacity, availability and quota gates.
The broader [resource admission design](RESOURCE_CONTROL.md) remains partly
unimplemented: tool/container leases and automatic retries are still absent;
proposal intake below supplies only policy-bound work selection.
The [unattended-operation roadmap](AUTONOMY.md) separates live capabilities,
fixture-tested extensions and staged implementation work; it changes no behavior. Explicit
checkpointed continuation and model handoff are described below.
Direct `dispatch.py submit` remains manually resource-gated.

## Launch a dedicated service with retained events

Requires Python 3.10+, Git, Bun, and this checkout's **already installed** source
dependencies. These commands install nothing. Run from `script/session-dispatcher`.
Keep host state, credentials, ledger, and task files outside Git worktrees.

```sh
export DISPATCH_STATE="$XDG_RUNTIME_DIR/opencode-dispatcher"
mkdir -p -m 700 "$DISPATCH_STATE/config/opencode"
```

Create `$DISPATCH_STATE/config/opencode/opencode.json` with your approved local
provider/model. For example, replace the port and model ID below with those of a
**separate, approved** local provider; submitting a task consumes that provider's
resources. Do not use active investigation sessions for the example.

```json
{
  "model": "local/approved-model",
  "providers": {
    "local": {
      "name": "Approved local provider",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": { "baseURL": "http://127.0.0.1:8040/v1" },
      "models": {
        "approved-model": {
          "name": "Approved model",
          "limit": { "context": 32768, "output": 4096 }
        }
      }
    }
  },
  "agents": {
    "title": { "model": "local/approved-model" },
    "compaction": { "model": "local/approved-model" }
  },
  "restrict_to_configured_providers": true,
  "security": {
    "mode": "strict",
    "allow_local": true,
    "allow_private_ip": false,
    "models_dev_enabled": false
  },
  "update": "disable",
  "share": "disabled",
  "warming": false
}
```

In one terminal, run:

```sh
bun --no-install ./serve.ts "$DISPATCH_STATE"
```

The source helper calls the existing `start({ events: { persist: true } })`.
It listens only on loopback, writes a mode-600 `connection.json`, and isolates
XDG data/config/cache/state and OpenCode's home/tmp paths. It does not register
with the normal managed service. Project config and Claude config are disabled;
do not add external packages/plugins to this dedicated host's configuration.
The minimal host does not install managed restart recovery. Restarting it retains
sessions/events but does not itself resume an interrupted assignment. Native
managed hosts can have their own restart/retry behavior; message admission does
not fence those effects. Run only one host against this state directory.

For durable storage across logouts, choose a private persistent directory rather
than `XDG_RUNTIME_DIR`. The host is **state isolation, not a filesystem sandbox**:
the agent still has the host user's tool access. Use a disposable repo for the
first task. Stop this host only after its assignments are idle.

An existing dedicated V2 service is also usable with a private JSON file
`{"url":"http://127.0.0.1:PORT","password":"..."}`. Its native event persistence
must already be enabled. The current CLI server default does not enable it;
there is no invented `--persist-events` flag. Approval probes the retained tail;
later missing sequences/watermarks stop completion verification.

## Approve one persistent session

In another terminal, use the same `DISPATCH_STATE`. First create a clean,
disposable Git repository with a committed `README.md` and a configured local Git
identity. Set its absolute canonical path:

```sh
export DISPATCH_WORKTREE=/absolute/path/to/disposable-repo
python3 - <<'PY'
import os
from pathlib import Path
from dispatch import Client
client = Client(Path(os.environ['DISPATCH_STATE']) / 'connection.json')
session = client.json('POST', '/api/session', {
    'title': 'Manual documentation task', 'agent': 'build',
    'model': {'providerID': 'local', 'id': 'approved-model'},
    'location': {'directory': os.environ['DISPATCH_WORKTREE']},
})['data']
print(session['id'])
PY
```

Save the printed ID as `DISPATCH_SESSION`. Alternatively select an existing,
independent **idle** session placed in the intended worktree. Approval pins its
session/project/model/agent, directory, Git common directory/branch/HEAD, and
event cursor. Pending forms/input, dirty worktrees, child sessions, and explicit
workspace placement are refused. Review the session's context before approving.

```sh
export DISPATCH_SESSION=ses_REPLACE_WITH_PRINTED_ID
python3 dispatch.py --db "$DISPATCH_STATE/ledger.sqlite3" approve docs \
  --connection "$DISPATCH_STATE/connection.json" \
  --session "$DISPATCH_SESSION" --worktree "$DISPATCH_WORKTREE"
```

Approval must be renewed after a completed task before assigning another: HEAD
and context have changed. An unresolved assignment blocks reapproval and new
tasks on the same session/worktree/branch. These are cooperating-dispatcher
reservations, not locks against humans, other clients, or filesystem writers.

## Submit, observe, and verify one task

Write a short assignment to `$DISPATCH_STATE/task.txt`, for example: “Clarify the
README introduction in one sentence. Preserve its meaning.” Then run:

```sh
python3 dispatch.py --db "$DISPATCH_STATE/ledger.sqlite3" submit docs-001 \
  --session docs --task-file "$DISPATCH_STATE/task.txt" --allow README.md
python3 dispatch.py --db "$DISPATCH_STATE/ledger.sqlite3" reconcile docs-001
python3 dispatch.py --db "$DISPATCH_STATE/ledger.sqlite3" status docs-001
```

`submit` saves the stable task/message ID and exact request **before its single
POST** with `delivery:"queue", resume:true`. Repeating the same command returns
the saved task without contacting the server. Reusing its ID with different
text, registry name, or allowed paths is rejected. `--allow` accepts exact files,
not globs or directories; repeat it for several files. This constrains result
verification and the prompt; it is not an agent permission policy.

Run `reconcile` manually as needed. It makes only GET requests, replays after the
saved exclusive cursor with `follow=false`, and atomically stores each event's
identity/hash, cursor, and derived facts. Reconnection continues from the last
committed event. `status` is offline **last-observed state**, not a heartbeat.

| State | Meaning / next step |
| --- | --- |
| `sending` | Request saved; admission may be unknown. Reconcile, never resend. |
| `admitted` | Admission receipt or retained enqueue exists; not proof of execution. |
| `running` | Own delivery observed and session not idle. Reconcile later. |
| `completed` | Attributable successful terminal event and idle snapshot; inspect Git. |
| `uncertain` | Missing evidence, drift, interruption, restart, failure, or mixed input. Inspect; no automatic retry. |
| `verified` | Explicit Git scope verification and attributable completion both passed. Human review still required. An enrolled unit retains its worktree reservation through learning. |
| `accounted` | Learning child settled separately, including failure/no-op; this does not claim a verified task commit. |

Take the full commit SHA from Git, independently of the assistant's prose:

```sh
git -C "$DISPATCH_WORKTREE" log -1 --format=fuller
python3 dispatch.py --db "$DISPATCH_STATE/ledger.sqlite3" verify docs-001 \
  --commit FULL_COMMIT_SHA
python3 dispatch.py --db "$DISPATCH_STATE/ledger.sqlite3" result docs-001 --patch
```

Verification requires a clean worktree on the pinned branch, HEAD equal to the
supplied full SHA, exactly one parent equal to the approved base, a nonempty diff
within allowed files, and the exact `OpenCode-Task: docs-001` trailer. The prompt
asks for that single normal commit. Result JSON includes state, base/commit/tree,
changed paths, and optionally the patch. Retrieval checks for subsequent drift.
This verifies **Git scope/provenance**, not semantic correctness, test success,
absence of non-Git effects, or exactly-once tool execution. Review before any
integration; this tool never merges.

If a commit exists despite a failed/missing terminal event, `verify` records the
Git proof while preserving `uncertain` and its reservation. Conversely, a terminal
success without a matching commit never verifies. Transport-only observation
failures can recover through `reconcile`; history gaps, mixed input, execution
restart, and actual binding drift remain flagged. The basic task path has no retry, abandon,
override, or lease-expiration command. Opt-in learning has a bounded **pre-admission**
GPU wait; expiry never releases an uncertain executing assignment. Stop for manual effect investigation; do
not invent a new task ID or edit the ledger to evade an unresolved reservation.

## API and evidence boundaries

Basic task dispatch only mutates `POST /api/session/:id/prompt`. Opt-in learning
also forks, configures and archives its disposable session, and makes an optional
scoped Git commit outside the learning mode; see [the lifecycle](LIFECYCLE.md). Registration and
reconciliation use `GET /api/session/:id`, `/api/session/active`,
`/api/session/:id/inbox`, `/api/session/:id/form`, and
`/api/experimental/session/:id/log?after=SEQ&follow=false`. Session creation above
is an explicit operator step. No direct inference API calls are made by the
dispatcher. HTTP proxies and redirects are disabled for the credentialed client.

The [native session contract](../../specs/v2/session.md) owns admission/delivery
and recovery semantics. Caller-supplied message IDs deduplicate **admission**;
repeating a native prompt with `resume:true` can still wake execution. Accordingly
this CLI never re-POSTs even that same ID after interruption. Neither a terminal
event nor its absence establishes whether a particular external effect occurred.
Native provider/engine retries are outside the dispatcher's control.

The ledger holds task text, snapshots, hashes and verification facts; protect it
as private data. It does not copy raw tool output, transcripts, credentials, or
the native SQLite store. It refuses an unrelated SQLite database. For deeper
local diagnostics reuse [provenance-audit](../provenance-audit/README.md) and the
existing [provenance documentation](../../specs/provenance-poc.md); audit or
permission records alone are not completion or network-transmission proof.
Keep all runtime artifacts outside Git. Log content is never executed or treated
as an instruction by this tool.

## Bounded shadow-review trial

`shadow.py` reuses this ledger, native session API, queue, pool occupancy, Git
verification and learning. It creates a fresh session and a small disposable
report repository per frozen delivery unit; it never forks the author. Reports
are advisory, excluded from integration handoffs and refill counts, and cannot
spawn another shadow. Delivery assignments admit before pending shadows; admitted
work/learning is never preempted. No new server, inference client or SQLite
migration is required (`queued.spec.purpose` is additive; old hashes stay intact).

Create a private policy outside project worktrees (actual paths/model required):

```json
{
  "version": 1,
  "database": "/private/dispatcher/ledger.sqlite3",
  "repository": "/path/to/project",
  "target_ref": "main",
  "root": "/private/dispatcher/shadows",
  "connection": "/private/dispatcher/connection.json",
  "model": {"providerID": "vLLM", "id": "EXISTING_MODEL"},
  "learning": {
    "prompt": "/path/to/project/.opencode/command/learn.md",
    "model": {"providerID": "vLLM", "id": "EXISTING_MODEL"},
    "allow": ["AGENTS.md"], "max_active": 4, "wait_seconds": 0
  },
  "max_cases": 10,
  "max_disagreements": 3
}
```

The root must already exist (0700), policy 0600. Enable `shadow_policy` in the
owned coordinator configuration. Its cycle captures one new ready delivery per
observation before the reference turn; additional units can be captured explicitly.
The coordinator freezes its own judgment before reading shadow output:

```sh
python3 shadow.py --policy "$SHADOW_POLICY" capture --task TASK_ID
python3 shadow.py --policy "$SHADOW_POLICY" reference --case CASE_ID \
  --input-hash FROZEN_HASH --assessment routine --evidence-file decision.txt
python3 shadow.py --policy "$SHADOW_POLICY" status
```

`capture --cold-replay --target PINNED_COMMIT` labels retrospective samples.
Fresh sessions can read only their frozen local bundle under native external-path
checks. They may edit `result.json`/`review.md`; their only allowed shell action,
`python3 finish.py`, validates unchanged inputs and commits those two files.
There is no arbitrary shell, network, container or native-test permission. Cases
pin source/task/learning/target, input/template/model and result commit identities.
Native session creation and queueing have durable intent; interrupted setup is
held, never automatically replayed. Inspect the case stage and existing native
session/queue before explicit recovery. A failed shadow does not fail its source.

Stop adding cases at ten units or three classification disagreements pending
adjudication; these are not automatically labelled material errors. Existing
cases keep observing and delivery continues. Agreement is not ground truth.
Missing references remain unpaired; Git ancestry never invents a reference verdict.
Frozen references cannot be overwritten. The bounded closure can lack context;
referenced files absent from the author tree fall back to the frozen pre-decision
target, with per-file origin commits recorded. The original task/dependency scope
is included; a deleted changed file is never revived by that fallback.
Target-only instructions, raw archives and copied reference files are evidence,
not fresh runtime success or standing authorization. Private state stays out of Git.

An explicitly renewed policy can instead set `continuous:true`, an absolute `until`,
`campaign`, and `max_cases` (at most 10,000). Comparisons then continue through
disagreements until the window/case budget ends; all references and disagreements
remain retained. A new campaign distinguishes prompt revisions and historical
retests from the original trial. No disagreement changes acceptance authority.

## Policy-bound automatic replenishment

`runner.py --autonomy /private/policy.json` runs `autonomy.py` inside the existing
observer. It consumes verified task+learning results, validates `followups.json`
against operator-authorized scopes/recipes, and enqueues through `Batch`. Qwen
also gets a bounded proposal-only assignment when the workset needs replenishing.
No Codex planning wake is required. Stable work keys deduplicate proposals; a
no-work planning result is not repeated until delivery progress changes the set.
The two additive `autonomy_item`/`autonomy_seen` tables live in the same ledger;
the existing queue, leases, task proofs, learning and dependencies remain authoritative.

The private version-1 policy requires `id`, `root` (existing0700), `database`,
`repository`, full frozen `base`, `connection`, `model`, `pool`, `service_state`,
`workers` (existing approved registry names), `protected_worktrees`, `learning`,
`until`, `backlog`, `max_tasks`, `max_worktrees`, `scopes`, and optional `seeds`.
Set `reuse_only: true` with `reuse_ref: "refs/heads/main"` for a fixed worker pool.
No new task worktree/session is created; intake waits when every listed worker is
active, reserved, dirty or has unintegrated work. After explicit review/integration
and cross-server-safe synchronization, an existing worker exactly at that branch
head can be reapproved for its next assignment. Branch identity and history remain
intact. Frozen reference pins still use `base`/verified parents; synchronization never
retargets inputs. It remains manual unless `sync_integrated_workers` is explicitly
enabled as described below. Without reuse-only, bounded provisioning
retains its original behavior. Shadow review repositories are separate disposable
report fixtures, not investigation worktrees.

Each seed/proposal has exactly:

```json
{"key":"stable-acceptance-gap","scope":"OPERATOR_SCOPE_ID","kind":"fixture",
 "objective":"A concrete existing correction or release-engineering gap and deliverable.",
 "references":["PLAN.md","existing/record/README.md"],"dependencies":[]}
```

Recipes `offline` and `fixture` authorize no native/container/network operations.
Optional `native_profiles` maps named recipes to explicit fixed execution bounds;
only scopes also listed in `native_scopes` may use them. This is standing operator
authorization, not a model-generated permission. Native fixes, installs, deployment,
unfixed fault replay and unknown profiles cannot be authorized by a proposal.
The shared task template is `templates/autonomy.txt`; already queued text is immutable.
Tasks output `{"tasks":[],"reason":"..."}` in their scoped `followups.json` when done.
Prerequisites use existing task IDs and `verified`, `integrated` or `approved`;
automatic intake never grants approval or treats a committed blocker as technical success.
References can also resolve through verified dependency receipts, with each file's
origin commit retained. Planning inputs include completed-output pins, allowed recipe
names and proposal rejection feedback. Unresolved native effects fence new native
proposals in that scope while unrelated scopes and offline inspection can continue.

Only idle, clean, unreserved existing contexts at the explicit base are reused.
Unintegrated results keep their exact branch heads for the existing integration gate.
If needed, bounded new linked worktrees and V2 sessions are created, with durable
pre-effect intent; failed/uncertain creation is held and never replayed. No container
is created by the refill code. `max_worktrees` bounds retained unreviewed branches;
reaching it is review backpressure, not permission to overwrite a completed unit.

Optional `shadow_policy` and `shadow_seeds` maintain a shadow backlog on the same
capacity. After three delivery admissions, a pending shadow gets priority when no
shadow is active; delivery/learning is never interrupted. Historical seeds can carry
`shadow_references` (prior immutable assessment, target and evidence) and are labelled
retrospective retests. New results without independent references remain **unpaired**.
Invalid advisory results are held per case and cannot stop independent delivery.

```sh
python3 runner.py --db "$DISPATCH_STATE/ledger.sqlite3" start \
  --run-dir "$DISPATCH_STATE/runs/weekend" \
  --until 2026-11-16T06:15:00+00:00 --autonomy /private/policy.json
python3 runner.py --db "$DISPATCH_STATE/ledger.sqlite3" status \
  --run-dir "$DISPATCH_STATE/runs/weekend" --live --inference-url http://localhost:8040
```

`--until` permits an explicitly bounded observation window up to seven days; pool
cutoffs still govern admission. Existing `--seconds` retains its one-day limit.
Restart retains the original deadline and exact policy hash; changing policy requires
an owned handoff and a new run directory. Status includes intake state counts.
Successfully completed execution at a retained, natively idle tail releases inference
capacity even if Git/learning review is held. Worktree/branch/evidence leases remain;
failed/interrupted execution, foreign input, missing retention and unknown outcomes
continue to reserve inference conservatively. No automatic retry or merge is added.

## Bounded unattended queue

`batch.py` adds two tables to the **same ledger** (initially version 3): frozen approved
assignments and provider pools. Existing task/session/worktree/branch checks,
message IDs, event cursors and the learning lifecycle remain authoritative.
No server, model client, container or second event store is created. Enable it
only after all cooperating dispatcher clients use a compatible version. The
queue and learning are in live use; new dependency gates below are separately
opt-in and require a controlled observer upgrade.

Approve each idle session as above, configure its provider pool, then enqueue
bounded assignments. Use a separate pool for the learning provider when needed.
Keep policy/task files private and unchanged after enqueueing. Set
`DISPATCH_UNTIL` to an explicit timestamp with UTC offset for the approved window;
provider IDs and connection files must match the existing service registry.

```sh
python3 batch.py --db "$DISPATCH_STATE/ledger.sqlite3" pool gpu \
  --connection "$DISPATCH_STATE/connection.json" --provider "$DISPATCH_PROVIDER" \
  --capacity 3 --state available --available-until "$DISPATCH_UNTIL"
python3 batch.py --db "$DISPATCH_STATE/ledger.sqlite3" enqueue docs-003 \
  --session docs --task-file "$DISPATCH_STATE/task.txt" --allow README.md \
  --learning-policy "$DISPATCH_STATE/learning-policy.json"
# Enqueue other independently approved sessions before starting the bounded run.
python3 batch.py --db "$DISPATCH_STATE/ledger.sqlite3" run --seconds 10800
python3 batch.py --db "$DISPATCH_STATE/ledger.sqlite3" status
python3 lifecycle.py --db "$DISPATCH_STATE/ledger.sqlite3" result docs-003
```

The runner verifies delivery/actual Git, gives learning first use of a released
slot, accounts for optional separate learning commits, and admits further
preapproved tasks as capacity permits. `ready` means a reviewable integration
unit, **not** source correctness, passing runtime tests or an integrated branch.
Use `review.py` for a separate bounded structured review; merging remains manual.

### Bounded coordinator wake-ups

`coordinator.py` is an optional notifier for the same ledger, not another
dispatcher. It batches unmerged verified units or current holds/forms and wakes
one approved Codex thread through the **existing daemon**. Batch/Lifecycle still
own Qwen admission, learning, reservations and verification. The notifier never
merges, chooses technical work or grants permissions; its maintained instruction
is `templates/coordinator.txt`.

Create a private configuration directory/file outside worktrees (0700/0600),
with actual paths and an explicit end time. Example shape (replace placeholders):

```json
{
  "version": 1,
  "database": "/private/dispatcher/ledger.sqlite3",
  "repository": "/path/to/project",
  "target_ref": "main",
  "thread_id": "APPROVED_COORDINATOR_THREAD_ID",
  "codex_socket": "/home/USER/.codex/app-server-control/app-server-control.sock",
  "bun": "/absolute/path/to/bun",
  "deadline": "EXPLICIT_ISO8601_END_TIME_WITH_TIMEZONE",
  "interval": 15,
  "min_ready": 2,
  "max_wait": 180,
  "cooldown": 600,
  "max_wakes": 12,
  "backlog_target": 6,
  "refill_pools": ["gpu"],
  "urgent_cooldown": 30,
  "max_escalations": 4,
  "allow_resume": false,
  "allow_effort_override": false,
  "base_effort": "medium",
  "protected_worktrees": ["/path/to/reserved-investigation"]
}
```

```sh
python3 coordinator.py --config /private/coordinator/config.json start
python3 coordinator.py --config /private/coordinator/config.json status
python3 coordinator.py --config /private/coordinator/config.json stop
```

The single-notifier lock and durable receipt are beside the ledger, using
`.coordinator.lock` and `.coordinator.json` suffixes. Another config directory
cannot create a second owner or reset spent budget. Handoffs/logs stay in the
configuration directory. Restart retains target, seen events and uncertain
admissions; stop affects only the notifier, never Codex/OpenCode execution.
To account for an unresolved wake, stop the notifier, inspect native queue/history
and actual Git, then use `account --wake-id ID --reason 'evidence reviewed'`.
Accounting never resends, resets spent budget or certifies effects.

The bridge uses already-installed Bun's `ws` compatibility over the existing
Unix WebSocket, with owned socket/private-parent checks; the daemon's symlink
route is supported. No new server, Python package, nested `codex exec` or direct
inference API is used. The [Codex protocol](https://developers.openai.com/codex/app-server)
and schemas generated from the running binary define the API. Local 0.167.2
queue probes showed that adding to an idle queue can start generation before
an explicit start RPC. A fixture was interrupted by archival; its retained
history has no tool item, which is not a general no-effect proof. Internal queue
IDs can change, so a one-row snapshot does not prove deduplication across
consumption or safe replay. History uses `ThreadItemEntry` wrappers and adds
empty `text_elements`; the sanitized actual-record fixture covers both. Durable pre-send intent, native queue/history
reconciliation and no blind retry cover lost admission/start acknowledgments.
A start receipt proves admission, not tool effects or verified Git results.

A stored thread reporting `notLoaded` may be active on another app-server.
Do not auto-resume a foreground context through the wrong daemon. Default
`allow_resume:false` waits for a loaded owner; enable it only for a deliberately
dedicated coordinator. Active turns and pending human queue input are never
interrupted or reordered. Existing approval and quota boundaries still apply.

Queue pressure is opt-in: `backlog_target` defaults to zero, and a nonzero value
requires explicit `refill_pools`. An open pool with observed occupancy and fewer
independent pending assignments than that target wakes the coordinator to prepare
useful authorized follow-ups, even without a new completed unit. Dependent pending
work is reported separately by the ledger and does not count as independent backlog.
This is a planning signal, never an admission/resource override. No suitable work
can be a valid outcome. A stable queue fingerprint admits only one refill wake
until assignments are added, admitted or completed; clock ticks, learning phases,
instruction views and the coordinator's own Git changes do not repeat it.

Ready units needed by pending `integrated` prerequisites and backlog shortfalls
use `urgent_cooldown` (default: ordinary cooldown), bypassing batch-size/wait
delays. All native idle/human-queue checks, total wake budget and deadline remain.
This avoids a ten-minute review delay becoming the critical path of minute-long
worker tasks. Changing a live policy needs an owned notifier handoff; admitted
worker prompts and existing receipt history stay unchanged.

For a dedicated coordinator, `allow_effort_override:true` controls only its
future-turn effort, leaving model/permissions unchanged. Routine work defaults
to medium. `escalate --issue TASK_OR_BLOCKER_ID --risk RISK --reason EVIDENCE`
accepts only the configured Codex thread and reuses `review.RISKS` (high/xhigh).
The default remains one escalation. An explicit `max_escalations` permits more
distinct issues within the same total wake budget; existing escalations still
count, and the same stable issue cannot escalate twice. Requests are journaled
before updating the pending pointer; an unresolved request cannot be replaced,
and an interrupted pointer write retains its intent for inspection. Known operator-only
decisions remain blocked; escalation never provides approval. Qwen reasoning is
unchanged. No model calls are made just to poll status or repeat historical
manually accounted checkpoints. New technical task selection remains judgment
inside the approved roadmap; no autonomous vulnerability-discovery loop is added.

### Worktree activity before reconciliation or synchronization

An empty active list on the dispatcher host does not establish that a worktree
is idle: another OpenCode server can own a running session in the same directory.
Before changing a worktree or branch, check every known host:

```sh
python3 worktree_guard.py --db /private/ledger.sqlite3 --worktree /path/to/worktree
```

This read-only command combines all registry/pool connection files with managed
`service.json` and `service-*.json` registrations under `$XDG_STATE_HOME/opencode`
(default `~/.local/state/opencode`). Add `--connection /private/connection.json`
for unregistered standalone hosts, `--service-state DIR` for another state root,
and repeat `--worktree` for an inventory. It uses metadata GETs only, including
sessions outside the dispatcher registry, and resolves nested/symlinked session
directories. It never submits input, changes models, stops servers or edits Git.

Exit 0 means all selected directories have a clear session-activity snapshot;
exit 1 means blocked or unknown. A failed host probe is unknown, except an
unreachable managed registration whose Linux owner PID is definitely absent.
Even an old registration is probed before that exception. This is not complete
discovery of arbitrary unregistered servers, a filesystem lease, Git cleanliness
validation or ledger approval. Apply existing reservation/input/learning guards
and recheck immediately before writing. CPU-inference worktrees especially must
remain unchanged while their agents run; a routine sync request does not override
their ownership. This preflight does not replace or hot-reload the live observer.

### Operational status and bounded observer

Milestone 1 adds a read-only operational view and a process wrapper around the
same `Batch.tick()` loop. It adds no ledger tables, server or model calls.
Plain `batch.py status` retains its existing output. For operational JSON use
`status --operations`; add `--text` for a short view and `--live` for GET-only
native capacity/inbox/form snapshots:

```sh
python3 batch.py --db "$DISPATCH_STATE/ledger.sqlite3" status --operations --live --text
```

For a compact handoff of **unmerged frozen task + learning receipts**, without
rechecking agent worktrees or modifying the ledger:

```sh
python3 batch.py --db "$DISPATCH_STATE/ledger.sqlite3" status --review-ready \
  --repository /path/to/project --ref main --text
```

The target commit is frozen for that report. Machine-verified receipts already
reachable from it are counted and omitted; unverified deliveries, missing objects and a missing
task base are reported per unit. JSON also lists the original source commits and
paths. These are **not** the proposed target merge delta or a correctness review.
Use the existing integration preflight and review gates before merging; this
view neither releases worktrees nor changes approvals or scheduling.

The report distinguishes task and learning stages, names conservative capacity
reservations, counts pending forms without copying their text, and points to
immutable ready-unit commits. It reads a consistent in-memory copy so slow
native GETs do not hold a live ledger reader lock. Offline occupancy is **unknown**, not zero.
Native snapshots have observation timestamps; task `updated` is only a ledger
update time. Ready receipt references are historical, not fresh verification of
a worktree that may since have advanced. Prompts, tool output, credentials and
raw issue bodies are omitted; issue IDs point back to existing task status.
Multiple reasons for one reserved session count as one occupied slot. Accounted
historical failures retain their evidence and are distinguished from current
holds; manually closed checkpoints are not labelled verified deliveries.

Add an explicit loopback inference endpoint to check the actual backend as well:

```sh
python3 runner.py --db "$DISPATCH_STATE/ledger.sqlite3" status --live \
  --run-dir "$DISPATCH_STATE/runs/YOUR_RUN" --inference-url http://localhost:8040
```

This makes bounded GETs to `/health`, `/v1/models` and `/metrics`, refuses redirects
and submits no generation. It shows advertised models, current running/waiting
requests, KV-cache usage/capacity, preemptions and cumulative prompt/generation
counters when exported by vLLM. Missing metrics remain unknown. Task lines include
the event cursor and tool-result count for the current observed run; old attempts
are not counted as new work. Tool outcomes and token counters do not prove a
build/test passed. Status stays read-only, never reopens a pool or retries work.
Use `--json` for structured checks; exit 0 means the report was generated, even
when `inference.state` is unavailable. No raw error bodies or configuration labels
are printed. Health is a reachability check; KV capacity and idle memory do not
establish peak GPU headroom during real prefill, decoding or speculative work.
Validate the requested concurrency with real assignments after a server change.

Use one private run directory outside all registered worktrees. Set the existing
pool admission deadline first, then choose a longer observation lifetime for
draining work already admitted:

```sh
export DISPATCH_RUN="$DISPATCH_STATE/runs/night-001"
python3 runner.py --db "$DISPATCH_STATE/ledger.sqlite3" start \
  --run-dir "$DISPATCH_RUN" --seconds 14400 --interval 5
python3 runner.py --db "$DISPATCH_STATE/ledger.sqlite3" status --run-dir "$DISPATCH_RUN" --live
# Stop observation at the next tick boundary; this does not pause pools or cancel agents:
python3 runner.py --db "$DISPATCH_STATE/ledger.sqlite3" stop --run-dir "$DISPATCH_RUN"
# Restart/reconcile the same run, retaining its original absolute deadline:
python3 runner.py --db "$DISPATCH_STATE/ledger.sqlite3" start --run-dir "$DISPATCH_RUN"
```

A busy queue-writer lock defers the tick to the next existing interval; it does
not resend work or extend deadlines. Other errors still stop observation and
retain their type plus a bounded message in the private state receipt. Inspect
that receipt before restarting an exited observer.

`run` is the foreground equivalent of `start`, suitable for an existing process
supervisor. `start` detaches with a new POSIX session and a private `observer.log`;
fixtures verify it survives its launcher. It does not install supervision or
promise survival of host reboot, logout policy or destruction of its containing
process group/cgroup. Resume explicitly under the same run directory after those
events. A startup acknowledgement means ownership was recorded, not task success.
No startup error or unconfirmed startup is automatically retried.

The run directory holds immutable deadline/config plus per-attempt code hashes,
runtime/schema identity, PID/start/boot/namespace diagnostics, heartbeat, last
successful tick, and exit reason. Records are private and atomically replaced.
The ledger remains authoritative for assignments and effects. A stale heartbeat
or unverifiable owner is unknown; a released ownership lock without an exit
receipt is an unobserved exit. Missing PID visibility is not death or permission
to signal anything. `stop` is bound to one attempt and sends no process signal.
SIGTERM/SIGINT also stop only this observer at a tick boundary.

New runners and new `batch.py run` processes share a lifetime observer lock,
separate from the existing short ledger/tick lock. Another observer is refused,
including through a parent-directory path alias. **Already running older
observers do not hold this lock.** At first adoption, stop only the established
observer through its supported ownership/exit procedure, preserve native agents,
then start the new runner. Do not adopt an uncertain task, clear a hold or launch
a second observer merely because no new-format heartbeat exists.

Pausing admissions remains the existing explicit `pool ... --state paused`
operation, preserving its binding/capacity/external occupancy. Observation can
continue after that pause or the admission cutoff. Stopping observation does not
cancel native execution. Restart reconciles existing queued intents before new
admission; it never resends an uncertain prompt. The finite observer deadline
still applies: restart cannot extend it; a fresh window requires a new run
directory and explicit resource availability. Exit codes are 0 for complete or
requested observer stop (distinguished by `exit_reason`), 1 for blocked/error,
and 2 for paused admission or observation deadline.

M1 validation (2026-11-10): the 174-test full fixture run passed, followed by
a 21-test focused run after final status/snapshot/schema-guard refinements.
Disposable real-process fixtures cover launcher survival, duplicate owners,
SIGKILL/restart without resend, cutoff/draining, stop without agent cancellation,
and independent work around a hold. Status fixtures cover secrets, stale
heartbeats, read-only schema preservation and concurrent writes during slow GETs.
The current schema-5 live ledger was inspected through GET-only status; its
runner was not replaced and no live task, permission, model or pool was changed.

### Task-ID prerequisites

Add `--dependencies /private/prerequisites.json` to `enqueue`, for example:

```json
[
  {"task":"source-fix","when":"integrated"},
  {"task":"stock-comparison","when":"verified"},
  {"task":"release-choice","when":"approved"}
]
```

Every condition is required. `verified` means the existing Git/event verifier
passed and any enrolled learning unit is accounted (`ready`, including an
accurately recorded failed/skipped/no-change learning outcome). It does not
mean source correctness or release approval. `integrated` additionally checks
the final task/learning commit's actual ancestry in `refs/heads/main` in the
prerequisite's repository; use an explicit `"ref":"refs/heads/dev"` for another
local target branch. It works with manual integrations and does not merge or
infer approval from the integration table's stage.

`approved` adds an explicit operator decision bound to this dependent assignment
and the prerequisite's immutable verified-result fingerprint:

```sh
python3 batch.py --db "$DISPATCH_STATE/ledger.sqlite3" approve-dependency \
  docs-003 release-choice --reason 'Reviewed the specific result and decision'
```

Approval cannot replace verification/learning accounting or authorize a merge.
Status includes each edge's waiting/blocked reason and retained admission proof.
Unknown future task IDs wait; cycles (including forward-reference cycles) and
duplicate/invalid edges are rejected. Failed/deferred prerequisites hold their
descendants; unrelated work continues under unchanged resource reservations.
Definitions are immutable and included in idempotency checks. Satisfied commit
pins are appended as data to the submitted prompt and covered by its existing
prompt hash; the task's approved base is unchanged. To change an
unsubmitted assignment, `batch.py ... defer TASK --reason ...`, then explicitly
enqueue a new task ID. Never edit SQL to release an uncertain/admitted task.

The first nonempty dependency list atomically adds `queue_dependency` and sets
schema **6**. Existing specs/hashes are unchanged; old rows have no prerequisites.
Do not add dependencies to a ledger while an old observer is running: stop only
its recorded observer process at an owned handoff boundary, retain native agents,
back up the ledger, and restart observation with the reviewed new code first.
Older clients refuse schema 6 on open; that does not fence an already-running
old process. The live ledger was not migrated during prototype tests.

These gates do not synchronize a worktree, change its approved base, reapprove a
session or populate new technical work. Same-worktree follow-ups still need the
previous reservation released and a fresh explicit approval; they cannot be
pre-enqueued on a guessed future base. PLAN remains the technical roadmap;
SQLite owns task assignments, edges, approvals, event/Git proofs and learning
state. HANDOVER should retain reasoning/recovery pointers rather than duplicate
recoverable queue snapshots. Runtime/build evidence remains in its original records.

Capacity counts native active sessions, unresolved ledger admissions and learning
reservations. `--external N` reserves capacity used outside this host. Pools map
one provider on one host; coordinate provider aliases/other hosts manually. The
same ledger-file lock serializes queue/lifecycle writers, but cannot fence humans
or direct submitters. There is no tool-process/container ownership enforcement.

A retained, deferred stopped attempt distinguishes two reservations. Its task,
unit, worktree, branch, session and attempt evidence stay held until the task is
verified/accounted, exactly as before. Only its **inference** reservation ends,
and only when every one of these holds: the task's queued row is explicitly in
the `deferred` state (ready/active/blocked queue records and a missing queue
record keep their reservation); an attributable terminal failure or interruption
with known admission and delivery, no history gap, drift, issue or foreign
input; every continuation intent for the task superseded; the session proven not
natively active and free of pending input/forms; and the retained event tail
still at the reconciled cursor (the same settled-tail proof as continuation
planning). Active native sessions always count, and missing or unsettled
evidence, unknown admission/delivery, a missing terminal, or any
missing/non-superseded continuation keep the conservative reservation. Nothing
here resumes, edits or discards the retained record.

Queue states: the runner processes `pending` (admission), `active` (observation)
and `continuing` (handoff) rows each tick. Holds and admission/recovery errors
are scoped to their assignment and dependent tasks. Independent work continues
with the existing worktree/branch/session guards and conservative inference
reservations; no held attempt is resumed, edited or cleared. Provider quota and
availability still gate their whole provider pool. Database corruption and other
unhandled infrastructure errors stop the observer.

`phase=waiting` keeps bounded observation alive for prerequisites, explicit
approval, integration or capacity within an open window. Admitted work continues
to be observed after cutoff (`draining`); an expired window with only pending
work stops (`paused`). Once only held/failed dependent work remains, stop with
`blocked`/exit 1, never `complete`. The run deadline always applies. `deferred`
and `ready` rows stay untouched. Deferring a retained record is explicit: move its
queued row to `deferred` (moving it to `ready` is not the deferral protocol).
The record's worktree/branch/session reservations are held by the ledger
indexes and the submit/approve collision checks, not by the queue state, and a
deferred task cannot be re-approved for continuation.

Native structured quota errors pause the affected pool as `quota`; rate limits
use `limited`. Raw error bodies are not copied. Neither cooldown expiry nor a
healthy endpoint re-enables a pool. Explicit `pool ... --state available` is
required after checking availability. No inference is spent probing quota.
Observation can reconnect from its cursor, but failed/interrupted execution and
uncertain admission are never resent. The explicit continuation path below can
approve a stopped task's model handoff; ordinary drift checks remain intact.

Provider windows close **new admissions**, not observation. `draining` keeps
tracking already admitted tasks and learning, verifying Git and accounting for
completion even when their pool is closed. New tasks, learning prompts and
continuations still require an open pool; waiting learning retains its existing
bounded wait/skip policy. Once active units are accounted for, a closed window
with pending work becomes `paused` and the runner exits.

`run --seconds N` is a separate observer lifetime: choose it long enough to
cover completion after the admission cutoff (maximum 86400 seconds). Reconciliation
and polling make no model calls. Review retains the existing medium default and
explicit escalation; no extra model review is added to routine accounting.
Run exits 0 when all enrolled units are ready, 1 on review/uncertainty, and 2 on
its observation time bound or paused admissions with no active work. Stopping the observer never
cancels active inference/tools. Restarting it reconciles existing intents before
admission. A crash between queue claim and durable task intent requires review;
it never sends another prompt automatically. Other active work stays untouched.

There is no automatic reapproval, base retargeting or dependent-task generation.
Each approved pending assignment reserves its known session/worktree/branch;
a subsequent assignment there needs reviewed updated context/base approval.
Learning keeps its existing bounded wait/explicit-skip policy. Availability
windows are bounded deadlines, not a recurring calendar or GPU-utilization probe.

## Checkpointed task continuation

`recovery.py` extends the same ledger to version 4 with preserved attempt
snapshots. It applies only to enrolled, stopped **task** executions, before
learning. It neither adopts live investigations nor restarts learning forks.
Update all cooperating clients before enabling it; the live ledger is unchanged.

The target model must already be configured on the same existing service. Set
`DISPATCH_MODEL` to its model-reference JSON. Selecting a different provider is
an explicit handoff, not an implicit fallback or an inference health probe.

```sh
python3 recovery.py --db "$DISPATCH_STATE/ledger.sqlite3" plan docs-003 \
  --model "$DISPATCH_MODEL"
# Review the checkpoint and copy approval_hash into DISPATCH_APPROVAL.
python3 recovery.py --db "$DISPATCH_STATE/ledger.sqlite3" prepare docs-003 \
  --model "$DISPATCH_MODEL" --approve "$DISPATCH_APPROVAL"
python3 batch.py --db "$DISPATCH_STATE/ledger.sqlite3" run --seconds 10800
python3 recovery.py --db "$DISPATCH_STATE/ledger.sqlite3" status docs-003
```

Planning refreshes local observation but makes no API mutations. Preparation
reserves a new stable message/attempt ID without sending it; repeat preparation
with the same approval hash is idempotent. The runner waits for the target pool's
availability/capacity, rechecks the exact checkpoint, selects the approved model
once if needed, and sends one **continuation**, retaining the same task/session,
worktree, branch and original base. Existing partial edits, staging, untracked
scoped files and task commits are preserved. Each retained commit must be linear,
within scope and carry the original task trailer. An already complete commit
is verified without recreating it. The original learning policy remains pinned.

If tools executed or partial work exists, preparation requires
`--effects-checked`: an explicit acknowledgement that the particular checkpoint's
external/background/tool effects have been inspected and settled. Git and a
terminal tool event cannot establish those effects generically. No command text
is replayed or used to guess that a shell command was harmless. Unfinished tools,
missing terminal events, foreign input, history gaps and out-of-scope changes
remain blocked even with that flag. There is no standing automatic retry policy.

Every prior receipt, binding and failure remains in the private attempt snapshots;
status/review expose attempt references. Old quota events remain attributed to
the original provider rather than pausing the replacement. An omitted variant
means the native default; the exact returned model binding is then pinned.
Lost admission receipts recover by observation, without another POST. Interrupted
model changes or unconfirmed admission without evidence remain held for review.
No container is created and no server/core configuration is changed.

If a stopped **original attempt has zero recorded tool events**, a coordinator
may explicitly accept a reviewed forward repository baseline. This is a narrow
exception to the default history guard, not an automatic retry:

```sh
python3 recovery.py --db "$DISPATCH_STATE/ledger.sqlite3" plan docs-003 \
  --model "$DISPATCH_MODEL" --accept-base "$REVIEWED_FULL_COMMIT"
# Inspect the listed commits/changed paths and git diff previous_base..accepted_base.
python3 recovery.py --db "$DISPATCH_STATE/ledger.sqlite3" prepare docs-003 \
  --model "$DISPATCH_MODEL" --accept-base "$REVIEWED_FULL_COMMIT" \
  --approve "$DISPATCH_APPROVAL"
```

The exact current HEAD must be clean, preserve the original history, leave
assigned output paths unchanged, and have no unfinished Git operation. Any tool
event, prior started handoff, foreign input or incomplete evidence refuses this
path; `--effects-checked` cannot override it. Review the diff against the task's
pinned inputs: their scope/hashes are never rewritten by this command.
An unsent waiting or `checkpoint_blocked` intent is superseded with a new stable
attempt/message ID; its original plan, approval and failed-attempt snapshot stay
intact. Preparation sends nothing. Admission rechecks the accepted checkpoint
and provider gate; only then does the new attempt adopt the accepted verification
base. Status includes both bases and the reviewed diff hash. Ordinary partial-work
continuation and all default drift checks remain unchanged. Already deferred or
replaced tasks are not adopted, so this cannot replay a completed replacement.

## Contracts and fixture notes

- The exact string `Execution failed/interrupted; effects may already exist` is
  a cross-module contract: `dispatch.event` writes it for an attributable
  terminal failure, and `recovery.plan_locked` and `batch.inference_held`
  require it. Rewording it requires updating all three.
- "Settled idle" is proven by `client.idle(session_id)` plus
  `client.tail(session_id) == task["cursor"]`. `Client.tail` requires an
  events-persist host and replays the full retained log, so `Batch.availability`
  pays that cost only for rows that pass the local ledger filter first.
- Ledger schema versions are opt-in: opening `Recovery(dispatcher)` without
  `batch=` creates the `attempt` table and bumps `user_version` to at least 4;
  batch-only ledgers are version 3 (fixtures assert that), so the version a
  ledger lands on depends on which module opened it first.
- When asserting ledger-row byte-equality in fixtures, snapshot after the last
  mutation: `Dispatcher.update` always rewrites `updated=now()`, and
  `recovery.plan` reconciles (a write) even when nothing changed.

## Disposable checks

```sh
python3 -m unittest -v test_dispatch test_lifecycle test_batch test_recovery test_integrate test_dependencies
python3 native_smoke.py
python3 native_smoke.py --learning
python3 native_smoke.py --batch
python3 native_smoke.py --recovery
```

The stdlib suite uses disposable Git repositories and a loopback HTTP contract
fixture: duplicate assignments, same-branch/worktree collisions, lost admission
receipt, interrupted SSE/reconnect, missing persistence, worktree/context drift,
foreign input, and uncertain completion with actual Git effects. The optional
smoke uses the current native source host, a deterministic loopback provider,
and real read/edit/shell tools in a temporary repository. The `--learning` variant
also exercises a real context-preserving fork, fork-only model selection, tool
filtering, ordinary queued learning, and a separate coordinator-owned commit. It verifies one queued
admission, native retained events, and a scope-checked commit. It needs existing
Bun/dependencies, installs nothing, and cleans up only its own child host/state.

Validated against fork source `8604ecc70ec314f444410d3646be1c9ca5552b8e`
(2.2.5): 21 fixture tests passed; the native smoke verified one queued admission,
four provider steps, retained replay through sequence 31, and the actual Git
commit. The experimental log API can change; rerun both checks after upgrades.

Queue checkpoint (2026-11-05, source base `12c8d420a238`): 65 stdlib fixture
tests passed; `native_smoke.py --batch` passed with eight fake-provider steps,
retained task cursor 31, Git verification, a real learning fork and its separate
commit. No live quota exhaustion, task handoff or automatic merge was exercised.

Continuation checkpoint (2026-11-05, source base `7f6b02523c`): 65 existing
checks and 25 continuation checks passed. The native `--recovery` fixture
interrupted its own post-commit execution, continued the same session on another
fixture model without recreating the commit, and completed separate learning:
nine fake-provider steps, two task admissions, retained task cursor 36. Actual
free-provider quota exhaustion and live-investigation handoff were not exercised.

Dependency checkpoint (2026-11-09, source base `1150b4bd7223`): 159 disposable
fixtures passed with Python 3.14.4 / SQLite 3.46.1 / Git 2.53.0. Coverage includes
v5 compatibility and opt-in v6, cycles, immutable changes, verification/learning,
actual Git ancestry, scoped provider/recovery failures, observer approval pickup,
crash-before-POST and lost responses. No live schema migration or dependency
dispatch, model invocation, core/API change or container modification was tested.

A coordinator wake budget is cumulative in its durable receipt, including across
restarts. Exhaustion stops notifications, not running worker tasks. For an
explicitly approved longer window, increase `max_wakes` (1..512) in the existing
private config and restart that notifier; spent wakes, per-issue escalations,
thread binding and the absolute deadline remain intact. It never renews itself.
Batch routine reviews with `min_ready`, `max_wait` and `cooldown`; extending the
budget does not authorize additional technical scope or guaranteed Codex quota.

For a reuse-only approved worker pool, `sync_integrated_workers: true` enables
bounded automatic `git merge --ff-only` to `reuse_ref` before the next assignment.
It never integrates a task into main: every frozen task/learning tip for that worker
must already be reachable from the reviewed main. It preserves branch/session
identity and checks reservations, all-server activity, unfinished Git operations,
HEAD/status drift and ignored-file collisions immediately before writing. Protected
CPU/GLM/benchmark contexts are excluded. A successful synchronization is recorded in
the next item's existing ledger detail as `worker_sync`. Dirty, active, divergent or
uncertain workers stay untouched. Default is disabled; enable it only in a new
immutable policy/run snapshot, preserving already admitted assignments.
