# Security Upstream Playbook

This document is a practical handoff for submitting local security work to upstream later.
It is written for both humans and coding agents.

## Goal

Avoid large hard-to-review PRs and reduce merge conflicts against `upstream/dev`.

## Scope

Use this flow for security-related changes in:

- provider URL/policy enforcement
- tool URL enforcement (for example `webfetch`)
- network validation utilities
- audit logging tied to security decisions

## Required upstream process

Per `CONTRIBUTING.md`, core product features need design review before implementation for upstream.
For local prototyping this can be done first, but before upstream PRs you should open a design issue.

## Recommended PR split

Create one design issue covering all parts, then land small PRs in this order:

1. Foundation: shared network/security utilities + unit tests.
2. Provider guardrails: provider URL enforcement + provider-side tests.
3. Tool guardrails: `webfetch`/tool URL enforcement + tool tests.
4. Audit/docs follow-up: audit field consistency, docs, migration notes.

If a part is too big, split again by file ownership.

## Git workflow for later upstreaming

### 1) Start from a clean upstream base

```bash
git fetch upstream
git checkout -b sec-upstream origin/dev
```

### 2) Bring local work into this branch

If your work is one large commit:

```bash
git cherry-pick -n <local-security-commit>
git reset
```

If your work is many commits:

```bash
git cherry-pick -n <first-commit>^..<last-commit>
git reset
```

`git reset` keeps file changes but unstages everything so you can create clean slices.

### 3) Build each PR slice as its own commit

Example:

```bash
# PR 1 foundation
git add packages/opencode/src/util/network.ts packages/opencode/test/util/network.test.ts
git commit -m "feat(security): add network URL validation primitives"

# PR 2 provider
git add packages/opencode/src/provider/provider.ts packages/opencode/src/provider/models.ts packages/opencode/test/provider
git commit -m "feat(provider): enforce URL security policy for provider endpoints"

# PR 3 tools
git add packages/opencode/src/tool/webfetch.ts packages/opencode/test/tool/webfetch.test.ts
git commit -m "feat(tool): enforce URL security policy in webfetch"
```

Adjust file lists to actual changes.

### 4) Verify each slice independently

Run relevant tests after each commit, not only at the end.
Keep each commit mergeable and reviewable on its own.

### 5) Submit as stacked PRs

Open PRs in order. Mention dependency chain in each PR description.

## Safety rules during review/learn loops

When using coding agents:

1. Before `/review` or `/learn`, save a snapshot:

```bash
git stash push -u -m "pre-review $(date -Iseconds)"
```

2. Use explicit constraints in prompt:
   - "Review only, do not implement."
   - "Do not run git revert/reset/checkout."
   - "Only edit AGENTS/docs for /learn."
3. If output looks like re-implementation, stop and restore:

```bash
git stash list
git stash show -p stash@{0} | less
git stash apply stash@{0}
```

## Design issue template (short form)

- Problem: current risk/threat model and why config-only controls are insufficient.
- Proposal: URL policy module + provider guardrails + tool guardrails + audit semantics.
- Compatibility: default behavior, strict mode behavior, migration path.
- Rollout: PR split plan and tests per slice.
- Open questions: anything that may affect architecture or policy defaults.

## Notes for future contributors

- Prefer small PRs touching hot files (`provider.ts`, `session/prompt.ts`, `tool/*`).
- Keep local/private policy (`opencode.json`, local scripts) out of upstream PRs.
- Rebase frequently on `upstream/dev` while preparing the stack.
