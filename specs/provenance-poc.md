# Provenance POC (OpenCode)

This is a minimal reference implementation for agent provenance and incident traceability.

## What it logs

When enabled, OpenCode writes JSONL events for:

- `session.created`
- `session.deleted`
- `session.error`
- `session.retry`
- `session.compacted`
- `command.executed`
- `assistant.completed`
- `tool.completed`
- `tool.error`

## Output files

Default location:

- `~/.local/state/opencode/provenance/events.jsonl`
- `~/.local/state/opencode/provenance/incidents.jsonl`

If `XDG_STATE_HOME` is set, state paths are resolved under that directory.

`incidents.jsonl` receives only non-`info` events (`warn` / `error`).

## Why this is low-footprint

- Uses existing internal bus events (no extra model/tool protocol changes).
- Stores concise metadata with hashes + previews, not full payload dumps.
- Keeps incident stream separate from full event stream for fast triage.

## Configuration

In `opencode.json` or `opencode.jsonc`:

```json
{
  "provenance": {
    "enabled": true,
    "preview_chars": 240
  }
}
```

Optional custom path:

```json
{
  "provenance": {
    "enabled": true,
    "path": "/var/log/opencode/provenance/events.jsonl",
    "preview_chars": 320
  }
}
```

## Built-in analysis prompt

Use `/provenance` to run incident/learning analysis over provenance logs.

The command is read-only and focuses on:

- recurring incidents
- non-obvious learning patterns
- actionable guardrails
- missing compliance evidence

`/learn` is also updated to optionally incorporate recent provenance incidents when extracting reusable AGENTS.md learnings.

## Compliance relevance (practical, not legal advice)

- **Traceability**: ties commands/tool outcomes/errors to sessions and timestamps.
- **Accountability**: records model/provider/agent outcomes at assistant completion.
- **Incident response**: dedicated incident stream enables quick failure analysis.

For stricter environments, add retention policy, encryption-at-rest, and signed export workflow on top of this baseline.
