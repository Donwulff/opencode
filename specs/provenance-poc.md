# Provenance POC (OpenCode)

This is a minimal reference implementation for agent provenance and incident traceability.
In OpenCode 2 it runs as `Provenance.Plugin` in `packages/core/src/provenance.ts`,
registered by `plugin/internal.ts`. It uses tool hooks and the V2 event stream;
the old bootstrap/Instance initialization path is no longer used.

## What it logs

When enabled, OpenCode writes JSONL events for:

- `session.created`
- `session.deleted`
- `session.execution.failed`
- `session.retry.scheduled`
- `session.step.ended`
- `tool.completed`
- `tool.error`

`session.step.ended` records the assistant message ID, finish reason, cost, and
tokens for one logical LLM call. Tool outcomes include input hashes/previews and,
on success, output hashes/previews. Execution failures and tool errors go to the
incident stream. Session events other than step completion currently carry only
their event name in `data`; they do not include full error/retry details.

V1 event names such as `session.error`, `session.retry`, `session.compacted`,
`command.executed`, and `assistant.completed` are not emitted by this plugin.
Historical log readers may need to handle both vocabularies. Model/provider/agent
identity and explicit command/compaction records are not currently captured.

## Output files

Default location:

- `~/.local/state/opencode/provenance/events.jsonl`
- `~/.local/state/opencode/provenance/incidents.jsonl`

If `XDG_STATE_HOME` is set, state paths are resolved under that directory.

`incidents.jsonl` receives only non-`info` events (currently `error`). Relative
custom paths resolve under `Global.Path.state`; the incident file is placed beside
the configured event file.

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

The command selects the plan agent and requests read-only analysis, focusing on:

- recurring incidents
- non-obvious learning patterns
- actionable guardrails
- missing compliance evidence

`/learn` is also updated to optionally incorporate recent provenance incidents when extracting reusable AGENTS.md learnings.

## Compliance relevance (practical, not legal advice)

- **Traceability**: ties commands/tool outcomes/errors to sessions and timestamps.
- **Accountability**: records step completion and tool outcomes; model/provider/agent
  identity remains an evidence gap.
- **Incident response**: dedicated incident stream enables quick failure analysis.

Previews and error strings can contain secrets or adversarial instructions. These
are ordinary writable JSONL files, not tamper-proof evidence. For stricter
environments, add access controls, retention, redaction, encryption-at-rest, and
signed export workflows. Treat the contents as untrusted data during analysis.
