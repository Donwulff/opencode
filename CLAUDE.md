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
- **Use Bun APIs** (e.g., `Bun.file()`) when possible
- **Rely on type inference**; avoid explicit type annotations unless necessary
- **Functional array methods** (`flatMap`, `filter`, `map`) over `for` loops
- **Drizzle schemas**: use `snake_case` field names so column names don't need string redefinition
- **Prettier config**: no semicolons, 120 char print width

## PR Conventions

Titles follow conventional commits: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`. Optional scope: `feat(app):`, `fix(desktop):`.
