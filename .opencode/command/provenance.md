---
description: analyze provenance incidents and learnings
agent: plan
---

Analyze OpenCode provenance logs and extract high-value incidents + learnings.

Hard scope rule:

- Read-only analysis only.
- Do not edit files unless explicitly asked in a follow-up.

Primary paths:

- `~/.local/state/opencode/provenance/events.jsonl`
- `~/.local/state/opencode/provenance/incidents.jsonl`

If `XDG_STATE_HOME` is set, use `$XDG_STATE_HOME/opencode/provenance/` instead.

Arguments:

- If `$ARGUMENTS` is provided, treat it as one or more extra paths to include.

Analysis goals:

1. Incident summary
   - Group by kind/severity
   - Highlight repeated failures and first-seen/last-seen timestamps
2. Learning extraction
   - Identify recurring tool misuse patterns
   - Identify model/agent/mode combinations associated with incidents
   - Identify process gaps (missing checkpoints, weak prompts, retry storms)
3. Actionable changes
   - Propose concise guardrails or workflow changes
   - Prefer minimal, high-leverage fixes
4. Compliance evidence quality
   - Call out what traceability is present
   - Call out key missing fields for stronger auditability

Output format:

- `Incident Overview`
- `Top Recurring Patterns`
- `Recommended Guardrails`
- `Missing Evidence`
- `Next 3 Checks`

$ARGUMENTS
