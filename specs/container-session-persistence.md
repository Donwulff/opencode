# Container Session Persistence Status

## Current behavior

This note describes the current behavior of the Docker analysis runner in
`docker/opencode-run.sh` and the OpenCode storage paths it mounts.

### Storage paths used by opencode

- `Global.Path.data` -> `~/.local/share/opencode`
- `Global.Path.state` -> `~/.local/state/opencode`

Important files under those roots:

- Session DB: `Global.Path.data/opencode.db`
- Audit log: `Global.Path.data/audit.jsonl`
- App logs: `Global.Path.data/log/*.log`
- Provenance POC: `Global.Path.state/provenance/events.jsonl`
- Provenance incidents: `Global.Path.state/provenance/incidents.jsonl`
- Other mutable UI/runtime state:
  - `Global.Path.state/model.json`
  - `Global.Path.state/kv.json`
  - `Global.Path.state/prompt-history.jsonl`
  - `Global.Path.state/prompt-stash.jsonl`

### What the Docker runner mounts

`docker/opencode-run.sh` now creates a stable per-project host directory:

- `${STATE_BASE}/projects/${PROJECT_KEY}/share`
- `${STATE_BASE}/projects/${PROJECT_KEY}/state`

and mounts them to:

- `/home/coder/.local/share/opencode`
- `/home/coder/.local/state/opencode`

It also creates a timestamped per-run directory:

- `${LOG_BASE}/${RUN_ID}`

### Result

- `audit.jsonl` is already persisted on the host today.
- `opencode.db` is persisted on the host today.
- Provenance POC files under `Global.Path.state` are persisted across `--rm` runs.
- Prompt/UI state under `Global.Path.state` is also persisted across `--rm` runs.
- `--continue` can reuse prior project sessions across separate container runs.
- Per-run metadata stays separate under `${LOG_BASE}/${RUN_ID}`.

## Shared-account behavior

For work use on a shared service account, resuming or inspecting another user's
session is a valid and expected workflow. The design should not try to
artificially block that.

The real question is operational safety:

- Can multiple opencode instances point at the same persistent session DB?
- If they can, is the resulting UX acceptable?

## Current concurrency status

OpenCode uses SQLite with:

- `PRAGMA journal_mode = WAL`
- `PRAGMA busy_timeout = 5000`

This means basic concurrent DB access is supported at the SQLite level.

However, there is no confirmed single-session locking model in the current app
for "session actively open by another operator". Shared visibility is likely to
work better than true simultaneous editing of the same live session.

For now, assume:

- shared session history in one persistent DB is technically plausible
- simultaneous editing of the exact same active session is not a designed,
  well-defined multi-user feature

## Audit immutability

Current status:

- Audit logging is persisted outside the container because `audit.jsonl` lives in
  `Global.Path.data`, which is mounted from the host.
- This does **not** by itself guarantee immutability against the LLM session,
  because the mounted data directory is writable by the container user and the
  app process itself writes into it.

So the current runner gives:

- persistence: yes
- strong immutability / tamper-resistance: not yet

That remains a separate hardening task.

## Storage split

1. Mutable project runtime state

- per-project persistent `share`
- per-project persistent `state`
- supports `--continue`, prompt state, provenance POC state, and session DB reuse

2. Per-run operational logs

- timestamped run/build logs under `logs/analysis/<run-id>/`

3. Audit evidence

- persisted on the host
- should eventually move toward append-only / tamper-resistant handling
- separate hardening problem from basic session persistence

## Notes

- This solves the practical problem first:

- sessions continue across container restarts
- provenance/state survives `--rm`
- shared-account users can resume shared project sessions

- The default project key is derived from the same git identity OpenCode uses
  for `project.id`:
  - cached `.git/opencode` value when present
  - otherwise the sorted first/root commit hash
- Non-git directories fall back to a resolved-path hash.
- `--project-id` exists as an override for moved repos or deliberate state reuse.
- This does **not** by itself solve audit immutability, which should be treated
  as its own follow-up hardening task.
