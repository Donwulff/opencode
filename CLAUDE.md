# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

OpenCode is an open-source AI coding agent with a client/server architecture. The core is written in TypeScript, runs on **Bun**, and uses the Vercel AI SDK for LLM integration. It supports multiple providers (Anthropic, OpenAI, Google, Azure, Bedrock, local models, etc.) and offers a TUI (terminal UI), web app, and desktop app as frontends.

## Build & Development

```bash
bun install                  # Install dependencies
bun dev                      # Run TUI (operates in packages/opencode by default)
bun dev <directory>          # Run TUI against a specific directory
bun dev .                    # Run TUI against repo root
bun dev serve                # Start headless API server (port 4096)
bun dev serve --port 8080    # Start on custom port
bun dev web                  # Start server + open web interface
```

**Typecheck:** `bun turbo typecheck` (from root) or `bun run typecheck` (from package dir). The `opencode` package uses `tsgo`.

**Build standalone binary:** `./packages/opencode/script/build.ts --single`

**Regenerate SDK after API changes:** `./script/generate.ts` (run this after modifying `packages/opencode/src/server/server.ts`)

**Regenerate JS SDK:** `./packages/sdk/js/script/build.ts`

## Testing

Tests run per-package, **not from root** (`bun test` at root will error).

```bash
cd packages/opencode && bun test                           # All opencode tests
cd packages/opencode && bun test test/tool/bash.test.ts    # Single test file
cd packages/app && bun test                                # App unit tests
cd packages/app && bun run test:e2e                        # Playwright e2e tests
cd packages/app && bun run test:e2e:ui                     # E2e with interactive UI
```

## Repository Structure

This is a Bun workspace monorepo managed with Turborepo. Key packages:

- **`packages/opencode`** — Core business logic, LLM orchestration, tools, server, and TUI
- **`packages/app`** — Web UI components (SolidJS + Vite)
- **`packages/desktop`** — Native desktop app (Tauri, wraps `packages/app`)
- **`packages/sdk/js`** — Generated TypeScript SDK for the API (auto-generated, do not edit by hand)
- **`packages/plugin`** — Plugin system (`@opencode-ai/plugin`)
- **`packages/ui`** — Shared UI primitives
- **`packages/web`** — Documentation site (Astro/Starlight)
- **`packages/util`** — Shared utilities (`@opencode-ai/util`)

## Architecture (packages/opencode)

### Namespace Pattern

Code is organized into **TypeScript namespaces** that act as modules: `Provider`, `Session`, `Agent`, `Tool`, `Config`, `Storage`, `Bus`, etc. Each namespace typically lives in its own directory with an `index.ts` barrel.

### Key Subsystems

- **Provider** (`src/provider/`) — Loads and configures LLM providers via AI SDK. `provider.ts` contains `CUSTOM_LOADERS` for each provider. `models.ts` handles model metadata from models.dev.
- **Agent** (`src/agent/`) — Defines agent configurations (name, model, permissions, prompts). Built-in agents: `build` (full-access), `plan` (read-only), `general` (subagent).
- **Session** (`src/session/`) — Manages conversation sessions, message history, LLM streaming (`llm.ts`), compaction, retries, and system prompts.
- **Tool** (`src/tool/`) — Each tool implements `Tool.Info` with an `execute()` method. Tools: bash, read, write, edit, glob, grep, webfetch, websearch, task, etc.
- **Config** (`src/config/`) — Layered config: managed (enterprise) → user (`~/.config/opencode/config.json`) → project (`.opencode/config.json`). Uses JSONC format.
- **Server** (`src/server/`) — Hono HTTP server with routes under `src/server/routes/`. Exposes REST + SSE APIs.
- **TUI** (`src/cli/cmd/tui/`) — Terminal UI built with SolidJS + [OpenTUI](https://github.com/sst/opentui).
- **Bus** (`src/bus/`) — Internal event bus using `BusEvent.define()` for typed events.
- **Storage** (`src/storage/`) — File-based persistence with migrations.
- **MCP** (`src/mcp/`) — Model Context Protocol server integration.
- **LSP** (`src/lsp/`) — Language Server Protocol client for editor features.

### DI and State

- `App.provide()` for dependency injection context
- `Instance.state()` for per-instance cached state
- `Log.create({ service: "name" })` for structured logging

## Default Branch

The default branch is **`dev`**, not `main`. Use `dev` or `origin/dev` for diffs and PRs.

## Style Guide

- **No `try`/`catch`** where possible; prefer `.catch()` or Result patterns
- **No `any` type**; use Zod schemas for validation
- **No `else`** statements; use early returns
- **`const` over `let`**; use ternaries or early returns instead of reassignment
- **No unnecessary destructuring**; use dot notation (`obj.a` not `const { a } = obj`)
- **Prefer single-word variable names**; inline values used only once
- **Bun APIs**: upstream is actively migrating *away* from `Bun.file()` toward the `Filesystem` abstraction (`src/util/filesystem.ts`); prefer `Filesystem` for new code in files that already use it
- **Rely on type inference**; avoid explicit type annotations unless necessary
- **Functional array methods** (`flatMap`, `filter`, `map`) over `for` loops
- **Drizzle schemas**: use `snake_case` field names so column names don't need string redefinition
- **Prettier config**: no semicolons, 120 char print width

## PR Conventions

Titles follow conventional commits: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`. Optional scope: `feat(app):`, `fix(desktop):`.

---

## Environment Architecture

This repo operates in a three-environment setup. Understand which environment you're working in before committing anything.

### 1. Public GitHub fork (this repo — `donwulff/opencode`)

Fork of upstream `opencode`. Contains security hardening, local model support, and other fork-specific features. Changes here can eventually become upstream PRs. **Nothing internal or environment-specific belongs here.**

### 2. Private infrastructure repo (company network)

A separate git repo that is never pushed to GitHub. Contains:
- OEL Dockerfiles with internal package lists, registry configs, subscription details
- `opencode.jsonc` configs with real LLM endpoint URLs and policy settings
- Experimental prompts and model-specific configs for local LLMs
- Build scripts that reference internal infrastructure

Pulls the public fork as a build input. Eventually hosted on company-internal git.

### 3. Local development environment

Where the actual running and testing happens. Local LLMs are accessed via localhost tunnels (the tunnel destination is an internal detail — do not document tunnel targets in any committed file).

Local model endpoint configs go in **`~/.opencode/opencode.jsonc`** — never in any git-tracked file.

---

### Non-Disclosure Rule — CRITICAL

**Never commit to this public repo:**

- IP addresses or hostnames, internal or external
- Port numbers or URL patterns that are environment-specific (including `localhost:PORT` if the port reveals something about the setup)
- Internal LLM model names, endpoint paths, or API key patterns
- Specific internal package names, versions, or Perl module lists
- Internal network topology or service names
- Anything that would tell an outside observer something specific about the internal production environment

**The test:** would a stranger reading this commit learn something specific about the internal environment? If yes, it does not belong here.

The localhost tunnel approach already anonymizes LLM endpoints — keep it that way. If something needs to be documented and it contains internal details, it goes in the private infra repo, not here.

**`.opencode/opencode.jsonc` in this repo** is upstream's project-level config for developing opencode itself. Any local model URLs or endpoint overrides that end up in `.opencode/opencode.jsonc` during local testing must be moved to `~/.opencode/opencode.jsonc` before committing. When in doubt, check `git diff` for IP addresses, port numbers, and hostnames before every commit.

---

## Fork-Specific Notes (donwulff/opencode)

This section documents fork-specific additions, known pitfalls, and hard-won knowledge from merging upstream changes.

### Recurring Upstream Merge Conflict Hotspots

These files have been merge-conflicted on every upstream sync. The pattern is predictable:
**upstream** is doing a systematic `Bun.file()` → `Filesystem` abstraction migration across the codebase; **the fork** adds security/audit features in those same files.

| File | Upstream change | Fork change |
|---|---|---|
| `src/provider/models.ts` | Migrate to `Filesystem` module | Security URL validation + audit logging |
| `src/provider/provider.ts` | Add new provider loaders (e.g., `kilo`) | `llama.cpp` local model loader |
| `src/tool/read.ts` | Migrate to streaming / `Filesystem` | `seen`-map cursor tracking for sequential reads |

**Strategy for each conflict:**
- **models.ts**: Keep both import sets (fork's `Config`, `auditLogger`, `validateUrlFromConfig`, `SecurityConfigType` AND upstream's `Filesystem`). In `refresh()`, keep the fork's full security validation block; use `modelsUrl` from the fork's block as the URL in fetch. At the bottom flag check, combine upstream's `--get-yargs-completions` guard with fork's `.catch(() => {})`.
- **provider.ts**: Keep both loaders. The `CUSTOM_LOADERS` object simply has both `"llama.cpp"` (fork) and `"kilo"` (upstream) entries.
- **read.ts**: The fork's `seen`-Map cursor tracking (session-aware cursor that auto-advances on sequential reads) is architecturally incompatible with upstream's streaming `createReadStream`/`readline` approach. The non-conflict code *after* the conflict markers uses `slice()`, `prev`, `result.*`, and `seen.set()` — all fork-specific — so **always take the fork's algorithm**. Replace `Bun.file().text()` with `await fs.readFile(filepath, "utf8")` since `Bun.file` gets removed in the non-conflicted section. Remove any dead `createReadStream`/`createInterface` setup that upstream may have added in the non-conflict section.

### Fork-Specific Features to Preserve on Each Merge

- **`src/provider/models.ts`**: Security validation of models.dev URL via `validateUrlFromConfig`; audit logging via `auditLogger`; security config type checks. These are the fork's primary value-add.
- **`src/provider/provider.ts`**: `CUSTOM_LOADERS["llama.cpp"]` — custom loader for local llama.cpp model discovery.
- **`src/tool/read.ts`**: `seen` Map for cursor-based sequential reads — allows the agent to call read repeatedly on the same file and auto-advance. The `MAX_BYTES_LABEL` constant from upstream is fine to keep alongside this.

### tsgo Union Narrowing Bug

The `opencode` package uses `tsgo` (native TypeScript preview compiler, ~7.0.0-dev). It has a confirmed bug with **progressive switch narrowing on large discriminated unions**: when the `Event` union has 43+ members, tsgo silently drops some members (notably `"message.part.delta"`) from the remaining type as the switch statement narrows.

**Symptoms**: `TS2678 Type '"message.part.delta"' is not comparable to type '...'` in a switch/case over `event.type`, even though the type clearly exists in the union definition.

**Two workarounds (both used in this codebase):**

1. **Cast at the source** (for `sdk.event.listen` callbacks in sync/TUI contexts):
   ```typescript
   import type { Event, ... } from "@opencode-ai/sdk/v2"
   // ...
   sdk.event.listen((e) => {
     const event = e.details as Event  // explicit cast bypasses tsgo's over-narrowing
     switch (event.type) { ... }
   })
   ```
   Used in: `src/cli/cmd/tui/context/sync.tsx`

2. **Extract to `if`-guard before the switch** (for large switch statements where the cast would lose narrowing for other cases):
   ```typescript
   private async handleEvent(event: Event) {
     // tsgo narrows away "message.part.delta" from the switch union, handle it first
     if (event.type === "message.part.delta") {
       // ... full handler ...
       return
     }
     switch (event.type) {
       // ... all other cases, but NOT "message.part.delta" ...
     }
   }
   ```
   Used in: `src/acp/agent.ts`

**Do NOT**: use `event.type as Event["type"]` — this breaks narrowing for all other cases inside the switch.

### TypeScript Narrowing via `.find()` Predicate

tsgo (and standard TS) cannot narrow union types through `.find()` predicates on arrays of union types. Pattern to use:

```typescript
// Wrong — TS cannot narrow the return type:
const command = [...input.messages].reverse().find((item) => item.info.role === "user")?.info.command

// Correct — explicit type guard after the find:
const found = [...input.messages].reverse().find((item) => item.info.role === "user")
const command = found?.info.role === "user" ? found.info.command : undefined
```

### Upstream `clone()` Removal

Upstream commit "remove unnecessary deep clones from session loop" removed `clone()` from `src/session/prompt.ts`. If merging introduces a `Cannot find name 'clone'` error, replace `clone(arr)` with `[...arr]` for shallow array copies (sufficient for `string[]` system prompts).

### Merge Workflow

After `git merge upstream/dev` produces conflicts:

```bash
# Check which files have conflicts
git diff --name-only --diff-filter=U

# After resolving all conflicts and staging:
git add packages/opencode/src/provider/models.ts \
        packages/opencode/src/provider/provider.ts \
        packages/opencode/src/tool/read.ts

# Commit the merge (use explicit message — --no-edit may fail if MERGE_HEAD is gone)
git commit -m "chore: merge upstream/dev"

# Always typecheck before pushing (pre-push hook runs typecheck)
bun turbo typecheck
git push
```

### Dockerfile.analysis (OEL Container for Code Analysis)

`Dockerfile.analysis` at the repo root builds a container for running opencode against bind-mounted code. Parametrized with `ARG OEL_VERSION` (default: 9).

```bash
# Build for OEL 9 (works on older hardware without AVX2)
docker build -t opencode-analysis:oel9 -f Dockerfile.analysis --build-arg OEL_VERSION=9 .

# Build for OEL 10 (requires AVX2 / x86-64-v3; glibc hard requirement)
docker build -t opencode-analysis:oel10 -f Dockerfile.analysis --build-arg OEL_VERSION=10 .

# Run against a local codebase
docker run --rm -it \
  --user $(id -u):$(id -g) \
  -v /path/to/code:/workspace \
  -e ANTHROPIC_API_KEY \
  opencode-analysis:oel9
```

**Important Docker ARG scoping**: `ARG OEL_VERSION=9` must be **redeclared after every `FROM` statement** in a multi-stage Dockerfile — Docker resets ARG values at each stage boundary.

**OEL 10 hardware requirement**: OEL 10's glibc requires the x86-64-v3 microarchitecture level (AVX2). CPUs without AVX2 (AMD FX/Piledriver 2012, Intel pre-Haswell 2013) will get `Fatal glibc error: CPU does not support x86-64-v3` and cannot run OEL 10 containers. Use OEL 9 on those hosts.

**ripgrep**: Not in default OEL repos; requires EPEL (`oracle-epel-release-el${OEL_VERSION}`). Install EPEL first, then ripgrep.

### Podman Rootless Lock Issue (OEL 8.x hosts)

If podman fails with `failed to open 2048 locks in /libpod_rootless_lock_1000: permission denied`:

```bash
ls -la /dev/shm/libpod*   # Check ownership — may be owned by a service user (e.g., mysql)
sudo rm /dev/shm/libpod_rootless_lock_*
```

Root cause: A third-party install script (MariaDB ColumnStore, etc.) ran `podman` as a service user, creating `/dev/shm/libpod_rootless_lock_1000` owned by that user. The "2048" is a fixed-size pre-allocated POSIX SHM semaphore pool. Subsequent rootless podman runs by your own user can't open the file. Safe to delete — podman recreates it on next run.
