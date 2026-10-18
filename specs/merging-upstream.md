# Merging upstream OpenCode 2

The fork tracks `upstream/dev` on `dev`; use `origin/dev` for ordinary fork diffs
and PR targets. Continue an existing merge in place rather than starting another
update/build helper while `MERGE_HEAD` exists.

## What V2 means

OpenCode 2 changes the plugin API, server/client contracts, and terminal client
configuration. Runtime code moved out of the former `packages/opencode` monolith:

| Owner                                                  | Current location               |
| ------------------------------------------------------ | ------------------------------ |
| Public domain/config schemas                           | `packages/schema`              |
| Session execution, config, permissions, tools, plugins | `packages/core`                |
| HTTP endpoint contracts                                | `packages/protocol`            |
| HTTP implementation and assembled `HttpApi`            | `packages/server`              |
| Generated Effect/promise clients                       | `packages/client`              |
| CLI/build and terminal UI                              | `packages/cli`, `packages/tui` |
| SDK composition                                        | `packages/sdk`                 |

See [V2 contracts](v2/README.md) and the
[migration guide](../services/www/src/docs/content/migrate-v1.mdx). Code under
`core/src/v1` is compatibility code, not the active V1 runtime. A filename containing
`v2` by itself is not evidence that a fork hook remains active.

Keep runtime dependencies directed from Schema to Core/Protocol and then Server.
Client may use Schema/Protocol but not Core/Server; SDK composes the layers.

## Resolution and verification

1. Record the two exact heads before completing the merge:

   ```sh
   merge_ours=$(git rev-parse HEAD)
   merge_upstream=$(git rev-parse MERGE_HEAD)
   git diff --name-only --diff-filter=U
   ```

2. Read root/package `AGENTS.md` and, when present, local `CLAUDE.md` and
   `CLAUDE-MERGE.md`. Resolve upstream structure first, then preserve fork behavior
   at the new call sites. Do not restore obsolete wrappers solely to eliminate a
   conflict. Keep private overlay documents out of public commits.

3. Install dependencies **before checking**:

   ```sh
   bun install
   ```

   Changes to workspace manifests, lockfiles, patches, or package additions can
   otherwise produce cascades of misleading missing-module/implicit-any errors.
   Inspect the dependency's actual resolution: historical `@opencode-ai/*` names
   can refer to vendored `file:` tarballs rather than a same-named workspace.

4. If Protocol, public Schema shapes used by the API, or Server `HttpApi` changed,
   regenerate from the merged source, including cleanly merged upstream changes:

   ```sh
   (cd packages/client && bun run generate)
   ```

   Do not hand-edit generated client files or use the retired
   `packages/sdk/js/src/v2/gen` workflow. Review and stage generator output.

5. Audit additions and renames **before committing**, even when Git reported only
   a few conflicts:

   ```sh
   git diff "$merge_ours" "$merge_upstream" --diff-filter=RA --name-status -- packages/
   git diff "$merge_upstream" --name-status -- packages/
   ```

   Confirm every upstream added/renamed destination exists in the resolution and
   contains the new behavior. The second diff reveals retained fork changes and
   unexpected missing upstream files. Review deletions separately; fork-only files
   absent upstream can be intentional. New network/spawn routes need security
   review even if they merged cleanly.

6. Sweep tracked source for markers, then run the canonical checks and focused tests:

   ```sh
   git grep -n -E '^(<<<<<<< |=======|>>>>>>> )' -- '*.ts' '*.tsx' '*.md' '*.json' '*.jsonc' '*.sh'
   bun run check
   (cd packages/core && bun run script/test.ts \
     test/instruction-discovery.test.ts test/plugin/websearch.test.ts test/voice.test.ts \
     test/network-policy.test.ts test/audit.test.ts test/skill-discovery.test.ts \
     test/tool-webfetch.test.ts test/plugin/provider-restrict.test.ts \
     test/plugin/provider-autodiscover.test.ts test/provenance.test.ts \
     test/config/normalization.test.ts test/config/config.test.ts test/config/reload.test.ts \
     test/session-projector.test.ts test/session-runner-retry.test.ts test/model-retry.test.ts)
   ```

   Review marker matches: some fixtures intentionally contain markers. Run changed
   tool/session/server tests as appropriate. Core's runner isolates HOME and XDG;
   config tests also walk ancestors, so an unrelated `.agents` or `.claude` directory
   above the temporary root can contaminate expectations. Use a clean temporary
   root rather than deleting unrelated files. Network fixtures require permission
   to bind loopback ports. Read `.opencode/skills/unit-tests/SKILL.md` before changing
   tests. Tests must run from package directories.

7. Review the resolved diff, stage only intended files, verify no unmerged entries
   remain, and finish with a conventional merge message:

   ```sh
   git diff --check
   git diff --cached --check
   git diff --name-only --diff-filter=U
   git commit -m "chore: merge upstream/dev and preserve security hardening"
   ```

   Publishing is separate from local resolution. No Changesets are used.

## Fork hooks to preserve

Paths in this table are relative to `packages/core/src` unless qualified.

| Concern                        | Implementation and active callers                                                                                                                                                                                             |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Config compatibility           | `packages/schema/src/config{,/security,/provider}.ts`, `config/normalize.ts`, `v1/config/migrate.ts`; keep security/provenance/restriction and provider override fields                                                       |
| Managed precedence             | `config.ts` keeps managed settings last; `Config.latest` consumers must use that effective value                                                                                                                              |
| URL policy and auditing        | `util/security.ts`, `util/network.ts`, `util/audit.ts`; check callers, not only definitions                                                                                                                                   |
| Instructions                   | `config/plugin/instruction.ts`; preserve upstream glob/home-path handling, watchers, explicit sources, and reload caching; check policy before fetch and evict denied cached content                                          |
| Skills                         | `config/plugin/skill.ts` passes security into `skill/discovery.ts` for catalog and file downloads                                                                                                                             |
| Search                         | All five `plugin/websearch/*` providers; TinyFish must check before initialize as well as search                                                                                                                              |
| Fetch                          | `tool/plugin/webfetch.ts` checks before I/O and sanitizes output                                                                                                                                                              |
| Voice                          | `voice.ts` gates actual setup requests with Location config; do not rely on provider registry filtering                                                                                                                       |
| Provider restriction/discovery | `plugin/provider/restrict.ts`, `plugin/provider/autodiscover.ts`, and registrations in `plugin/internal.ts`; restriction remains guarded against plugin removal                                                               |
| Catalog                        | `models-dev.ts` guard and registration in `config.ts`; bundled snapshot now exists and can remain usable with network refresh disabled                                                                                        |
| Shell                          | `shell.ts` must call `shell/sandbox.ts` before Environment spawn; tool/plugin/shell keeps command guard and post-approval audit                                                                                               |
| Command restrictions           | `tool/command-guard.ts`, tool plugins for shell/edit/write/patch/subagent; command survives Schema → admission → delivery → SessionStore                                                                                      |
| Provenance                     | `provenance.ts`, `plugin/internal.ts`, tool execute hooks and current session event names                                                                                                                                     |
| Other fork behavior            | Provider `system_prompt` in `plugin/optimize.ts`, agent `plan_reminder` in config/plan plugins, debug/review/learn commands, shared retry delay cap in `model-retry.ts` plus the post-plugin cap in `session/runner/retry.ts` |

The [security analysis](../SECURITY-ANALYSIS.md) documents coverage limits. Neither
URL hooks nor shell namespacing cover every V2 network/process path.

## October 2026 merge lesson

The `ebed9529f3` → `07bbd0fd16` merge conflicted in instruction loading, instruction
path documentation, and TinyFish imports. Upstream instruction loading now caches
remote content and retains it on transient fetch failure. Putting the policy check
inside that fallback would retain content after a policy denial: validate before
fetch/fallback and return no cached content on denial.

TinyFish adds an authenticated MCP handshake; keep the check before its first
request. The cleanly added Voice service also introduced an outbound path outside
provider filtering, requiring its own request gate. The rename/add audit is what
makes those changes visible even when they produce no conflict markers.

The subsequent merge to `501b15af26` extracts retry scheduling into `model-retry.ts`,
used by both session steps and buffered generation. Preserve the fork's 61-second
provider delay cap there, and keep the final cap after session retry plugins can
override the delay. Keeping only the session cap would leave buffered generation
with upstream's 15-minute maximum.
