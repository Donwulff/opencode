# Fork Changes: donwulff/opencode

This document tracks all changes this fork adds on top of upstream `opencode`.
Updated against: `upstream/dev` as of 2026-05-02.

To regenerate the file list: `git diff upstream/dev...dev --stat`

---

## Ownership Boundary

This fork intentionally splits changes into two classes:

### Public/upstream-trackable changes

These belong in the GitHub repository and should be documented and reviewed here:

- reusable OpenCode product changes
- provider/tool/runtime behavior changes
- prompts and commands intended to travel with the fork
- tests and specs for product behavior
- upstreaming notes in `specs/security-upstream-playbook.md`

### Local/private operational changes

These stay in the local/private overlay and should not be prepared as upstream PRs:

- `docker/` analysis container build/run files
- local launcher scripts and service-account operational workflow
- company-specific threat model or deployment assumptions
- internal endpoint/config examples
- container/session persistence policy for the private analysis environment

Rule of thumb:

- if the change is generally useful to OpenCode users, keep it in the public repo
- if the change depends on local infrastructure, service-account workflow, or
  internal security practice, keep it in the private overlay

The session-persistence design for the private analysis container is documented in:

- `specs/container-session-persistence.md`

The upstreaming split for security-related product changes is documented in:

- `specs/security-upstream-playbook.md`

---

## Feature Index

| # | Feature | Status | Config key | Key files |
|---|---------|--------|------------|-----------|
| 1 | [Security & Network Validation](#1-security--network-validation) | Complete | `security` | `util/network.ts`, `util/audit.ts`, `tool/bash.ts`, `util/process.ts` |
| 2 | [Provenance & Event Tracing](#2-provenance--event-tracing) | POC/Complete | `provenance` | `provenance/index.ts` |
| 3 | [Local llama.cpp Support](#3-local-llamacpp-support) | Complete | `provider.llama.cpp` | `provider/provider.ts` |
| 4 | [Read Tool: Cursor Tracking](#4-read-tool-cursor-tracking) | Complete | — | `tool/read.ts` |
| 5 | [Glob: Case-Insensitive Search](#5-glob-case-insensitive-search) | Complete | — | `tool/glob.ts` |
| 6 | [Prompt & Config Overrides](#6-prompt--config-overrides) | Complete | — | `.opencode/*.txt` |
| 7 | [Retry Backoff with Jitter](#7-retry-backoff-with-jitter) | Complete | — | `session/retry.ts` |
| 8 | [Safety Launcher](#8-safety-launcher) | Complete | — | `local-opencode-safe.sh` |
| 9 | [Commands: /review, /learn, /provenance](#9-commands-review-learn-provenance) | Complete | — | `.opencode/command/` |
| 10 | [TUI: Mouse Toggle & Slash Commands](#10-tui-mouse-toggle--slash-commands) | Partial | — | `tui/app.tsx` |
| 11 | [Documentation & Specs](#11-documentation--specs) | Complete | — | `CLAUDE.md`, `specs/`, `packages/web/` |

---

## 1. Security & Network Validation

**Status:** Complete
**Config key:** `security` in `opencode.json`

### What it does

Adds an enterprise security layer with two complementary mechanisms:

**HTTP-level validation**: Every URL used by a tool (webfetch, websearch, provider model fetches, instruction URLs, skill discovery) is validated against a configurable policy before the request is made. Blocked requests are audit-logged.

**Subprocess network isolation** (`OPENCODE_SPAWN_SANDBOX=1`): All subprocess spawns (bash tool, ripgrep, grep) run in a Linux user+network namespace so they have no outbound network access. This is the primary defense against data exfiltration via the bash tool — the subprocess has no network interface to send data through. bwrap (bubblewrap) is preferred; unshare is used as fallback. The flag is baked into `Dockerfile.analysis` as `ENV OPENCODE_SPAWN_SANDBOX=1`. Loopback is brought up inside the namespace so localhost test servers remain reachable.

**Read-only command enforcement**: `command-guard.ts` enforces read-only mode for `/review` and `/learn` commands, blocking file writes, shell redirection, file-mutating commands, and destructive git operations.

### Key files

| File | Role |
|------|------|
| `packages/opencode/src/util/network.ts` | Core policy engine: IP range validation (IPv4/IPv6 CIDR), private/localhost detection, domain allowlist/blocklist, DNS suffix matching, security mode enforcement |
| `packages/opencode/src/util/audit.ts` | Structured JSONL audit logger; logs URL checks, provider loads, model access, tool requests, sub-agent invocations, and denials to `~/.local/state/opencode/audit.jsonl` |
| `packages/opencode/src/util/fetch.ts` | `auditedFetch()` / `auditedUrl()` — validates + logs all internal `fetch()` calls (instruction URLs, skill discovery); also used by Effect-based pipelines |
| `packages/opencode/src/tool/command-guard.ts` | Read-only guard for review/learn sessions; blocks file writes, shell redirection, destructive git operations |
| `packages/opencode/src/tool/webfetch.ts` | Validates + audit-logs before every fetch; validates redirects too |
| `packages/opencode/src/tool/websearch.ts` | Validates MCP endpoint URLs before search requests |
| `packages/opencode/src/provider/models.ts` | Blocks models.dev fetch if security config disallows external URLs; audit-logs provider loads |
| `packages/opencode/src/tool/bash.ts` | Subprocess sandbox via `sandboxedArgs()` — wraps bash spawn in bwrap/unshare network namespace |
| `packages/opencode/src/util/process.ts` | `sandboxedCmd()` — wraps all `Process.spawn` calls (grep, ripgrep) in the same network namespace |
| `packages/opencode/src/flag/flag.ts` | `OPENCODE_SPAWN_SANDBOX` flag definition |

### Configuration

```jsonc
// .opencode/config.json or ~/.config/opencode/config.json
{
  "security": {
    "mode": "internal-only",          // "internal-only" | "external-allowed" | "strict"
    "allow_local": true,
    "allow_private_ip": true,
    "allow_external_ips": false,
    "models_dev_enabled": true,
    "audit_log_enabled": true,
    "external_domains": ["*.anthropic.com", "*.openai.com"],
    "block_domain_regex": [".*\\.shadow\\.net"],
    "allow_internal_dns_suffixes": ["corp.internal"],
    "allowed_ip_ranges": ["10.0.0.0/8", "fc00::/7"]
  }
}
```

### Tests

`packages/opencode/test/util/network.test.ts` (582 lines), `audit.test.ts` (172 lines)

### Known gaps

- No encryption-at-rest for audit logs
- No built-in log rotation/retention policy
- IPv6 link-local and carrier-grade NAT (169.254.x.x, 100.64.x.x) handling present but less tested
- cloud metadata endpoint (169.254.169.254) not explicitly blocked by default

---

## 2. Provenance & Event Tracing

**Status:** POC/Complete (functional; export workflow TBD)
**Config key:** `provenance` in `opencode.json`

### What it does

Records a structured event log for every session: tool calls (with input/output hashes + previews, never full content), assistant completions (provider, model, token counts, cost), errors, and retries. A separate incidents log captures only warn/error events for fast triage. A `/provenance` command provides an LLM-assisted incident analysis workflow.

### Key files

| File | Role |
|------|------|
| `packages/opencode/src/provenance/index.ts` | Event bus subscriber; writes `events.jsonl` and `incidents.jsonl`; deduplicates repeated events; stores content hashes + previews |
| `specs/provenance-poc.md` | Design spec: log structure, use cases, data model |
| `.opencode/command/provenance.md` | `/provenance` command: read-only plan-agent prompt for incident analysis and learning extraction |

### Output files

- `~/.local/state/opencode/provenance/events.jsonl` — full event stream
- `~/.local/state/opencode/provenance/incidents.jsonl` — warn/error events only

### Logged event types

`session.created`, `session.deleted`, `session.error`, `session.retry`, `session.compacted`, `command.executed`, `assistant.completed`, `tool.completed`, `tool.error`

### Configuration

```jsonc
{
  "provenance": {
    "enabled": true,
    "path": "optional/custom/path.jsonl",  // default: ~/.local/state/opencode/provenance/
    "preview_chars": 240
  }
}
```

### Tests

`packages/opencode/test/util/provenance.test.ts`

### Known gaps

- No compliance export workflow (users must build their own)
- No data retention/rotation policy
- `/learn` command integration with provenance incidents is mentioned but not fully wired

---

## 3. Local llama.cpp Support

**Status:** Complete
**Config key:** `provider.llama.cpp`

### What it does

Adds a custom provider loader for local [llama.cpp](https://github.com/ggml-org/llama.cpp) servers. Auto-discovers available models from the running server's `/v1/models` endpoint, reads context window sizes and capabilities, and wraps them using the OpenAI-compatible SDK. Also adds a `llama-slot` plugin tool for managing llama.cpp's KV-cache slot system to speed up multi-turn sessions.

### Key files

| File | Role |
|------|------|
| `packages/opencode/src/provider/provider.ts` | `CUSTOM_LOADERS["llama.cpp"]`: fetches model list, maps capabilities, creates provider with OpenAI-compatible wrapper |
| `.opencode/tool/llama-slot.ts` | Plugin tool: list, save, restore, erase llama.cpp cache slots |
| `.opencode/tool/llama-slot.txt` | Tool description loaded by plugin system |
| `opencode-local` | Bash launcher: merges upstream, builds, runs opencode with optional flags |

### Configuration

```jsonc
{
  "provider": {
    "llama.cpp": {
      "options": {
        "baseURL": "http://127.0.0.1:8080"
      }
    }
  }
}
```

### Capability defaults (when not reported by server)

- Context window: 128 000 tokens
- Capabilities: text generation, tool calling
- No: reasoning, audio, vision, image generation
- Cost: zero (local execution)

### Known gaps

- No fallback/retry if server is unreachable at startup (logs warning, `autoload=false`)
- Slot tool requires server to be started with `--slots --slot-save-path <dir>`

---

## 4. Read Tool: Cursor Tracking

**Status:** Complete
**Merge conflict risk:** HIGH — conflicts with upstream's `Bun.file()` → `Filesystem` migration on every sync

### What it does

Adds a `seen` Map that tracks read state per file path across a session: what offset was last requested, whether the result was truncated, and where the cursor ended. This enables the agent to read a file incrementally without re-specifying offsets, and detects when the same content is being re-requested unnecessarily.

### Key files

| File | Role |
|------|------|
| `packages/opencode/src/tool/read.ts` | `seen: Map<string, {requested, limit, truncated, cursor, explicitOffset}>` |

### Merge conflict strategy

See `CLAUDE.md` § "Recurring Upstream Merge Conflict Hotspots" for the detailed resolution strategy. Short version: always take the fork's algorithm; replace `Bun.file().text()` with `fs.readFile()`; remove upstream's dead `createReadStream`/`createInterface` setup.

---

## 5. Glob: Case-Insensitive Search

**Status:** Complete

### What it does

Adds `--iglob` support to the glob tool so patterns can match files regardless of case. Results are sorted by modification time (newest first) and capped at 100 with a truncation notice.

### Key files

| File | Role |
|------|------|
| `packages/opencode/src/tool/glob.ts` | `--iglob` flag, mtime sort, 100-file cap |
| `packages/opencode/src/tool/glob.txt` | Updated tool description |

---

## 6. Prompt & Config Overrides

**Status:** Complete (repo-specific prompts)

### What it does

Project-level prompt files in `.opencode/` customize agent behavior for this repo's specific workflows. `llama4.txt` enforces autonomous problem-solving and research discipline for Llama 4 models. `plan-reminder.txt` provides a detailed five-phase planning workflow for `/plan` mode. Provider-specific prompt overrides can also be set in `opencode.json`.

### Key files

| File | Role |
|------|------|
| `.opencode/llama4.txt` | System prompt for Llama 4: autonomous research, tool-first, step-by-step planning |
| `.opencode/plan-reminder.txt` | `/plan` workflow: understand → design → review → finalize → execute |
| `packages/opencode/src/session/prompt.ts` | Fork additions: `/prompt` command, provider prompt overrides |
| `packages/opencode/src/config/config.ts` | `SecurityConfig` + `provenance` config schemas added to the config type |

### Merge conflict risk

`session/prompt.ts` is a frequent conflict site. See `CLAUDE.md` for known tsgo narrowing workarounds that apply here.

---

## 7. Retry Backoff with Jitter

**Status:** Complete

### What it does

Replaces the upstream retry logic with exponential backoff capped at 61 seconds, plus up to 3 seconds of random jitter to avoid thundering-herd on rate limits. Respects `Retry-After` and `Retry-After-Ms` response headers. Context window overflow errors are detected and not retried (no point).

### Key files

| File | Role |
|------|------|
| `packages/opencode/src/session/retry.ts` | `RETRY_INITIAL_DELAY=2000`, `RETRY_BACKOFF_FACTOR=2`, `RETRY_MAX_DELAY=61000`, `RETRY_JITTER_MS=3000` |

---

## 8. Safety Launcher

**Status:** Complete

### What it does

`local-opencode-safe.sh` is a wrapper script that auto-snapshots the working tree before and during opencode runs (every 5 minutes by default), then blocks destructive git operations via a PATH-injected guard wrapper. Useful when running opencode against the repo itself. `opencode-local` is a lighter companion that merges upstream and builds before launching.

### Key files

| File | Role |
|------|------|
| `local-opencode-safe.sh` | Full safety wrapper: snapshots, guards, flags (`--no-update`, `--no-build`, `--snapshot-only`, `--mouse`) |
| `opencode-local` | Lightweight launcher: merge upstream, build `--single`, run |

### Snapshot locations

- `.scripts/opencode-safety/<timestamp>.tracked.patch` — tracked file changes
- `.scripts/opencode-safety/<timestamp>.untracked.tgz` — untracked files

### Local hygiene notes

- `bun install` at repo root can rewrite `package.json` and `bun.lock` by re-resolving versions such as `semver`. Treat those as intentional dependency bumps only if you meant to update them.
- `packages/opencode/script/build.ts` generates `packages/opencode/src/provider/models-snapshot.ts`, `packages/opencode/src/provider/models-snapshot.js`, and `packages/opencode/src/provider/models-snapshot.d.ts` locally. These are local build artifacts in this fork and are ignored.
- `Dockerfile.analysis` container build logs should live under `logs/analysis/` instead of the repo root.

### Known gaps

- Snapshot directory should be in `.gitignore` (add if not already)
- No automatic cleanup of old snapshots

---

## 9. Commands: /review, /learn, /provenance

**Status:** Complete

### What it does

Three read-only agent commands for governance workflows:

| Command | File | Purpose |
|---------|------|---------|
| `/review` | `src/command/template/review.txt` | Code review from diff, commit, or PR URL — enforces no-edit mode |
| `/learn` | `.opencode/command/learn.md` | Extracts non-obvious learnings from session history into AGENTS.md format |
| `/provenance` | `.opencode/command/provenance.md` | Incident analysis using provenance event logs |

All three use read-only plan-agent mode (`command-guard.ts` enforces this for `/review` and `/learn`).

---

## 10. TUI: Mouse Toggle & Slash Commands

**Status:** Partial

### What it does

Adds `OPENCODE_DISABLE_MOUSE` flag support to conditionally disable mouse event handling in the TUI (useful in some terminal emulators or screen sessions). Also adds `slash` metadata to built-in commands so they appear in the prompt autocomplete popover.

### Key files

| File | Role |
|------|------|
| `packages/opencode/src/cli/cmd/tui/app.tsx` | Mouse flag check, slash command metadata |
| `packages/opencode/src/flag/flag.ts` | `OPENCODE_DISABLE_MOUSE` flag definition |

---

## 11. Documentation & Specs

**Status:** Complete

### What it does

Substantial documentation additions:

| File/Path | Content |
|-----------|---------|
| `CLAUDE.md` | Fork-specific guidance: tsgo bugs, merge conflict strategies, Dockerfile usage, podman fix |
| `FORK-CHANGES.md` | This document |
| `Dockerfile.analysis` | OEL 9/10 container for running opencode against bind-mounted code |
| `specs/provenance-poc.md` | Provenance system design spec |
| `specs/security-upstream-playbook.md` | Playbook for merging upstream security-sensitive changes |
| `packages/web/src/content/docs/security.mdx` | Security documentation page (+ 20 language translations) |
| `packages/web/src/content/docs/config.mdx` | Security config fields documented |
| `packages/web/src/content/docs/tui.mdx` | TUI usage additions |
| `packages/opencode/src/*/AGENTS.md` | Per-subsystem agent guidance files |
| `packages/web/AGENTS.md` | Web package agent guidance |

---

## Merge Conflict Hotspots

Files that conflict on (nearly) every upstream sync:

| File | Upstream trend | Our addition | Strategy |
|------|---------------|--------------|----------|
| `src/provider/models.ts` | `Bun.file()` → `Filesystem` migration | Security URL validation + audit logging | Keep both; see CLAUDE.md |
| `src/provider/provider.ts` | New provider loaders (`kilo`, etc.) | `llama.cpp` loader | Keep both loaders |
| `src/tool/read.ts` | Streaming / `Filesystem` refactor | `seen`-map cursor tracking | Always take fork algorithm; see CLAUDE.md |
| `src/session/prompt.ts` | Ongoing refactors | `/prompt` command, provider overrides | Careful merge; watch tsgo narrowing |

See `CLAUDE.md` § "Fork-Specific Notes" for detailed resolution strategies and known tsgo compiler bugs.

---

## SDK Snapshot

`packages/sdk/js/openapi.json` (12 000+ lines) is a snapshot of the OpenAPI spec captured at a point in time. It is **not** auto-generated on build from this repo; it was committed manually. If the upstream API changes significantly, this will diverge. The generated client files under `packages/sdk/js/src/v2/gen/` are also snapshots.

To regenerate: `./script/generate.ts` then `./packages/sdk/js/script/build.ts`
