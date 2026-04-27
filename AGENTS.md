- To regenerate the JavaScript SDK, run `./packages/sdk/js/script/build.ts`.
- In sandboxed assistant runs, SDK generation can fail with `EACCES` on `~/.local/share/opencode/log/dev.log`; rerun the script with escalated permissions.
- ALWAYS USE PARALLEL TOOLS WHEN APPLICABLE.
- The default branch in this repo is `dev`.
- Local `main` ref may not exist; use `dev` or `origin/dev` for diffs.
- If `CLAUDE.md` and `CLAUDE-MERGE.md` are present in the working tree, read them — they hold fork-specific build, merge, and security guidance not in this file. Especially relevant before resolving any `upstream/dev` merge conflict.
- After any `upstream/dev` merge, run `bun turbo typecheck` and audit renames/adds (`git diff <our-merge-head> <upstream-head> --diff-filter=RA --name-status -- packages/opencode/`) before committing. Silent rename-misses do not surface as conflict markers and have shipped broken merges before.
- Prefer automation: execute requested actions without confirmation unless blocked by missing info or safety/irreversibility.
- For upstreaming local security work later, follow `specs/security-upstream-playbook.md` (design issue first, then split into small PRs).
- Provenance tracing POC docs and usage live in `specs/provenance-poc.md` and `.opencode/command/provenance.md`.

## Style Guide

### General Principles

- Keep things in one function unless composable or reusable
- Avoid `try`/`catch` where possible
- Avoid using the `any` type
- Use Bun APIs when possible, like `Bun.file()`
- Rely on type inference when possible; avoid explicit type annotations or interfaces unless necessary for exports or clarity
- Prefer functional array methods (flatMap, filter, map) over for loops; use type guards on filter to maintain type inference downstream
- In `src/config`, follow the existing self-export pattern at the top of the file (for example `export * as ConfigAgent from "./agent"`) when adding a new config module.

Reduce total variable count by inlining when a value is only used once.

```ts
// Good
const journal = await Bun.file(path.join(dir, "journal.json")).json()

// Bad
const journalPath = path.join(dir, "journal.json")
const journal = await Bun.file(journalPath).json()
```

### Destructuring

Avoid unnecessary destructuring. Use dot notation to preserve context.

```ts
// Good
obj.a
obj.b

// Bad
const { a, b } = obj
```

### Variables

Prefer `const` over `let`. Use ternaries or early returns instead of reassignment.

```ts
// Good
const foo = condition ? 1 : 2

// Bad
let foo
if (condition) foo = 1
else foo = 2
```

### Control Flow

Avoid `else` statements. Prefer early returns.

```ts
// Good
function foo() {
  if (condition) return 1
  return 2
}

// Bad
function foo() {
  if (condition) return 1
  else return 2
}
```

### Schema Definitions (Drizzle)

Use snake_case for field names so column names don't need to be redefined as strings.

```ts
// Good
const table = sqliteTable("session", {
  id: text().primaryKey(),
  project_id: text().notNull(),
  created_at: integer().notNull(),
})

// Bad
const table = sqliteTable("session", {
  id: text("id").primaryKey(),
  projectID: text("project_id").notNull(),
  createdAt: integer("created_at").notNull(),
})
```

## Testing

- Avoid mocks as much as possible
- Test actual implementation, do not duplicate logic into tests
- Tests cannot run from repo root (guard: `do-not-run-tests-from-root`); run from package dirs like `packages/opencode`.

## Type Checking

- Always run `bun typecheck` from package directories (e.g., `packages/opencode`), never `tsc` directly.
