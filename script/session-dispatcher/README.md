# OpenCode V2 dispatcher

A Python standard-library CLI for one explicitly assigned task at a time per
approved persistent session, physical worktree, and Git branch. It uses the
customized V2 API and native durable session log. It has no autonomous task selector, merge command, plugin, or OpenCode-core changes.
An opt-in [post-task lifecycle](LIFECYCLE.md) now accounts for learning forks and
reviewable integration units; `review.py` can invoke one bounded Codex CLI review.

`dispatch.py` owns a **separate private SQLite ledger**, not OpenCode's database.
Use one shared ledger for all cooperating operators. Different ledgers cannot
prevent each other's assignments. Registration is explicit: this tool never
discovers, starts, replaces, or interrupts a managed service or live session.

The opt-in bounded queue below drains approved assignments through the same
ledger and lifecycle, with provider capacity, availability and quota gates.
The broader [resource admission design](RESOURCE_CONTROL.md) remains partly
unimplemented: no task selection, tool/container leases, execution retry or
model handoff. Direct `dispatch.py submit` remains manually resource-gated.

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

## Bounded unattended queue

`batch.py` adds two tables to the **same ledger** (version 3): frozen approved
assignments and provider pools. Existing task/session/worktree/branch checks,
message IDs, event cursors and the learning lifecycle remain authoritative.
No server, model client, container or second event store is created. Enable it
only after all cooperating dispatcher clients use this version; older clients
refuse version 3. This prototype has not been enabled on active task ledgers.

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

Capacity counts native active sessions, unresolved ledger admissions and learning
reservations. `--external N` reserves capacity used outside this host. Pools map
one provider on one host; coordinate provider aliases/other hosts manually. The
same ledger-file lock serializes queue/lifecycle writers, but cannot fence humans
or direct submitters. There is no tool-process/container ownership enforcement.

Native structured quota errors pause the affected pool as `quota`; rate limits
use `limited`. Raw error bodies are not copied. Neither cooldown expiry nor a
healthy endpoint re-enables a pool. Explicit `pool ... --state available` is
required after checking availability. No inference is spent probing quota.
Observation can reconnect from its cursor, but failed/interrupted execution and
uncertain admission are never resent. Model handoff, including free-provider to
local-model continuation, is not implemented; existing drift checks stay intact.

Run exits 0 when all enrolled units are ready, 1 on review/uncertainty, and 2 on
its time bound or unavailable capacity/windows. Stopping the observer never
cancels active inference/tools. Restarting it reconciles existing intents before
admission. A crash between queue claim and durable task intent requires review;
it never sends another prompt automatically. Other active work stays untouched.

There is no automatic reapproval, base retargeting or dependent-task generation.
Each approved pending assignment reserves its known session/worktree/branch;
a subsequent assignment there needs reviewed updated context/base approval.
Learning keeps its existing bounded wait/explicit-skip policy. Availability
windows are bounded deadlines, not a recurring calendar or GPU-utilization probe.

## Disposable checks

```sh
python3 -m unittest -v test_dispatch test_lifecycle test_batch
python3 native_smoke.py
python3 native_smoke.py --learning
python3 native_smoke.py --batch
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
