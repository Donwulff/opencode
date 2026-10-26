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

   Use the Bun version pinned in root `package.json`. An older Bun can rewrite
   lockfile resolution/hoisting even when no dependency change was intended;
   inspect install-only changes rather than staging them automatically.

   `opencode-local` uses Bun from the caller's `PATH` and validates it before
   installing dependencies. It must not prepend `.scripts/runtime`: that ignored
   directory may contain an older Bun left behind after an upstream version bump.

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
   `packages/sdk/js/src/v2/gen` workflow. A conflict in generated types is resolved
   by fixing their source contracts first, then running the generator and staging
   its output. Do not combine the generated conflict blocks manually. Git keeps
   the path marked unmerged until the regenerated file is staged.

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
     test/session-prompt.test.ts test/session-projector.test.ts \
     test/session-runner-retry.test.ts test/model-retry.test.ts \
     test/session-title.test.ts test/session-compaction.test.ts \
     test/plugin/provider-opencode.test.ts test/usage-limits.test.ts)
   ```

   Review marker matches: some fixtures intentionally contain markers. Run changed
   tool/session/server tests as appropriate. Core's runner isolates HOME and XDG;
   config tests also walk ancestors, so an unrelated `.agents` or `.claude` directory
   above the temporary root can contaminate expectations. Use a clean temporary
   root rather than deleting unrelated files. Network fixtures require permission
   to bind loopback ports. Read `.opencode/skills/unit-tests/SKILL.md` before changing
   tests. Tests must run from package directories.

   Local checks use `bun check`; CI/publishing also use the official TypeScript
   compiler. Run `bun run typecheck:tsc` in an affected package when checking that
   path, and treat it as authoritative if the compilers disagree.

   When media admission/storage or its public routes change, also run Core's
   prompt/projector tests and Server's `test/media.test.ts` and
   `test/session-form.test.ts`. Verify command metadata survives both inline
   attachments and stored media references.

7. Review the resolved diff, stage only intended files, verify no unmerged entries
   remain, and finish with a conventional merge message:

   ```sh
   git diff --check
   git diff --cached --check
   git diff --name-only --diff-filter=U
   git commit -m "chore: merge upstream/dev and preserve security hardening"
   ```

   Upstream patch files can trigger whitespace warnings on their context lines.
   Verify those files are byte-identical to the recorded upstream head before
   attributing the warnings to upstream; keep the rest of the diff clean. Do not
   strip patch context whitespace to silence the check, since that can break
   dependency installation.

   Publishing is separate from local resolution. No Changesets are used.

## Local helper recovery

`opencode-local` snapshots its running script in memory so a merge that updates
the launcher cannot change the commands an already running shell reads. It also
checks for an existing `MERGE_HEAD` before stashing or starting another merge.
That recovery branch waits for resolutions to be staged and then commits them;
it does not run the generator, security tests, or repository checks for you.

Keep the conflict prompt waiting while resolving and verifying the merge in a
second terminal. Complete these checks before letting the helper commit, or
finish the merge manually and use `./opencode-local --no-update --no-run` to
install/build afterwards. `--no-build` skips both install and build. An interrupted
run can leave its original stash or temporary skip-worktree state to recover;
restarting the helper does not reconstruct that previous process's bookkeeping.
Inspect leftover `opencode-local: pre-merge stash ...` entries before restoring or
dropping them, including any untracked content. Stash-pop conflicts use
`Updated upstream` / `Stashed changes` marker labels.

For an unexpected Bun-version error, compare `command -v bun`, `bun --version`,
and root `package.json`'s `packageManager`. An old executable earlier in `PATH`
can select the wrong runtime without installing or downgrading anything. The
launcher validates Bun before dependency installation; never fix a version error
by prepending an older cached `.scripts/runtime` directory.

## Fork hooks to preserve

Paths in this table are relative to `packages/core/src` unless qualified.

| Concern                        | Implementation and active callers                                                                                                                                                                                             |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Config compatibility           | `packages/schema/src/config{,/security,/provider}.ts`, `config/normalize.ts`, `v1/config/migrate.ts`; keep security/provenance/restriction and provider override fields                                                       |
| Managed precedence             | `config.ts` keeps managed settings last; `Config.latest` consumers must use that effective value                                                                                                                              |
| URL policy and auditing        | `util/security.ts`, `util/network.ts`, `util/audit.ts`; check callers, not only definitions                                                                                                                                   |
| Instructions                   | `config/plugin/instruction.ts`; preserve upstream glob/home-path handling, watchers, explicit sources, and reload caching; check policy before fetch and evict denied cached content                                          |
| Skills                         | `config/plugin/skill.ts` passes security into `skill/discovery.ts` for catalog and file downloads                                                                                                                             |
| Search                         | All five `plugin/websearch/*` providers plus Console-hosted search in `plugin/provider/opencode.ts`; TinyFish must check before initialize as well as search                                                                  |
| Fetch                          | `tool/plugin/webfetch.ts` checks before I/O and sanitizes output                                                                                                                                                              |
| Voice                          | `voice.ts` gates OpenAI, OpenCode-hosted GPT-Live, xAI, and Gemini Live setup requests with Location config; do not rely on provider registry filtering                                                                       |
| Provider restriction/discovery | `plugin/provider/restrict.ts`, `plugin/provider/autodiscover.ts`, and registrations in `plugin/internal.ts`; restriction remains guarded against plugin removal                                                               |
| Catalog                        | `models-dev.ts` guard and registration in `config.ts`; bundled snapshot now exists and can remain usable with network refresh disabled                                                                                        |
| Shell                          | `shell.ts` must call `shell/sandbox.ts` before Environment spawn; tool/plugin/shell keeps command guard and post-approval audit                                                                                               |
| Command restrictions           | `session/command.ts`, `session/prompt.ts`, Schema, inbox/projector and SessionStore preserve `command`; `tool/command-guard.ts` enforces it in shell/edit/write/patch/subagent tools                                          |
| Provenance                     | `provenance.ts`, `plugin/internal.ts`, tool execute hooks and current session event names                                                                                                                                     |
| Other fork behavior            | Provider `system_prompt` in `plugin/optimize.ts`, agent `plan_reminder` in config/plan plugins, debug/review/learn commands, shared retry delay cap in `model-retry.ts` plus the post-plugin cap in `session/runner/retry.ts` |

The [security analysis](../SECURITY-ANALYSIS.md) documents coverage limits. Neither
URL hooks nor shell namespacing cover every V2 network/process path.

## October 2026 merge lessons

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

The merge to `450e7b9241` changes the AI package's Effect entry point to
`@opencode/ai/effect`. Update fork-only callers too: Git can leave their old imports
untouched. Public prompt types are now plain Schema structs; keep `command` in
prompt preparation, stored inbox/message shapes, and projector delivery without
restoring retired class wrappers. Regenerate conflicted client types from source.

That merge also adds Console-hosted search and a hosted GPT-Live fallback. Gate
the actual destination URL, including the hosted fallback, and test that denial
leaves the HTTP transport untouched. Console account/config/usage requests remain
outside these gates. Re-check background title and compaction model selection
and document its limits in the [privacy review](../SECURITY-ANALYSIS.md#model-routing-and-privacy-review-2026-10-25).

The merge to `7359a48573` moves prompt preparation, revert commit, and admission
into one Location-services acquisition in `session/session.ts`. Keep that owning
scope rather than restoring the old inner `Instance.provide` in
`session/prompt.ts`. `session/command.ts` now runs activation and command execution
inside its session's Location scope as well. Preserve the fork's `command` field
at command invocation, prompt-hook input, and inbox payload construction. A clean
typecheck cannot detect a missing optional field: exercise `/review` and `/learn`
through actual preparation/admission/delivery as well as the tool guards.

That merge also extends media blob admission, storage and client reads. Stored
and media-view schemas inherit the command field from the public prompt/message
schemas; preserve those spreads and projector delivery. Regenerate the client for
the new media API and check that both inline and stored-attachment paths retain
the fork metadata. The new catalog build helper reads the committed snapshot or
an explicit `OPENCODE_MODELS_SNAPSHOT` file; release snapshot fetching is separate
from the runtime `ModelsDev` URL guard.
