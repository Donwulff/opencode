# Security Analysis — donwulff/opencode fork

This document describes the fork's active OpenCode 2 protections and their limits.
For vulnerability disclosure, see [SECURITY.md](SECURITY.md). For merge procedures
and the current hook inventory, see [specs/merging-upstream.md](specs/merging-upstream.md).

## V2 scope

OpenCode 2 replaces the old `packages/opencode` runtime with separate Schema, Core,
Protocol, Server, Client, CLI, and TUI packages. Built-in tools and config loaders
are Location-scoped Effect plugins. The current implementations are under
`packages/core/src`, and public config types are under `packages/schema/src`.
`packages/core/src/v1` provides compatibility parsing and migration; preserving a
hook there alone does not protect the V2 runtime.

The aim is to analyze potentially untrusted code with explicit network policy and
an external isolation boundary. **The application is not deny-by-default without
configuration.** An absent `security` object permits URL requests at the fork's
policy call sites; an absent `OPENCODE_SPAWN_SANDBOX` flag leaves shell networking
unchanged. Container firewall rules, mounts, capabilities, and installed sandbox
utilities must be verified in the deployment separately.

## Active protections

Paths below are relative to `packages/core/src` unless stated otherwise.

| Surface                 | Active enforcement                                                                                                              | Limits                                                                                                                                                                                 |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `webfetch`              | `tool/plugin/webfetch.ts` calls `Security.assertUrl` before permission/fetch and sanitizes converted text                       | Initial URL only; redirects are not revalidated. Response size is bounded to 5 MiB.                                                                                                    |
| Built-in web search     | Exa, Parallel, Firecrawl, Tavily, TinyFish, and Console-hosted search check their endpoint before sending queries                | Search snippets are not sanitized. Console search rejects redirects; other providers and third-party plugins need separate review.                                                     |
| TinyFish authentication | The URL check precedes credential use, MCP initialize, initialized notification, and search                                     | Preserve that order when merging handshake changes.                                                                                                                                    |
| Remote instructions     | `config/plugin/instruction.ts` checks URLs and strips Unicode tag/zero-width characters before caching content                  | Config reload checks policy again. A denied URL loses its cached copy; an allowed URL with a failed fetch can retain the last good copy. Local instruction files remain trusted input. |
| Remote skill discovery  | `config/plugin/skill.ts` passes effective security settings to `skill/discovery.ts`; the catalog and file downloads are checked | Skill paths are also checked for traversal and cross-origin references. Redirects remain a gap.                                                                                        |
| Provider discovery      | `plugin/provider/autodiscover.ts` checks the model-list endpoint before requesting it                                           | Discovery protection is distinct from model inference transport.                                                                                                                       |
| Provider availability   | `plugin/provider/restrict.ts` disables unconfigured providers when requested and checks provider `settings.baseURL`             | Providers without that setting, model-specific endpoint overrides, and arbitrary transport calls are not comprehensively covered. This is a registry filter, not a network firewall.   |
| Model catalog refresh   | Config registers the `models-dev.ts` guard; it honors URL policy and `models_dev_enabled`                                       | The guard is process-global, installed by Location config; last registration wins. Startup before registration and multiple Locations are not a strong isolation boundary.             |
| Voice session setup     | `voice.ts` wraps the request executor with `Security.assertUrl` for the actual setup URL                                        | Covers OpenAI, OpenCode-hosted GPT-Live, xAI, and Gemini Live setup. Subsequent client media/WebSocket traffic is outside the server's URL checker.                                        |
| Shell network namespace | `shell.ts` calls `ShellSandbox.command` before spawning                                                                         | Linux only, opt-in, and limited to callers of `Shell.Service`. See below.                                                                                                              |
| `/review` and `/learn`  | `tool/command-guard.ts` reads the latest delivered user message's command from `SessionStore`                                   | Guards are command-specific and blocklist-based, not a general read-only sandbox.                                                                                                      |
| Managed security config | `config.ts` loads managed settings after project and inline settings; consumers use `Config.latest`                             | Managed configuration must actually be present and enabled. Permissions have their own ordering rules.                                                                                 |
| Audit logging           | URL gates record allowed/blocked decisions; the shell tool records approved commands                                            | Not a complete record of all HTTP, tools, or subprocesses.                                                                                                                             |
| Provenance              | `Provenance.Plugin` is registered in `plugin/internal.ts`; records tool outcomes and selected session events                    | Opt-in. See [provenance-poc.md](specs/provenance-poc.md) for current event names and missing evidence.                                                                                 |

The command metadata survives prompt admission and inbox delivery through Schema,
`session/prompt.ts`, and `session/projector.ts`. The guard is called by the shell,
edit, write, patch, and subagent tools. Patch resolution checks both source and
move destination. `/learn` permits file edits only when the target basename is
`AGENTS.md` and forbids subagents. Ordinary user input clears the command context.
The October 26 merge to upstream `7359a48573` moves preparation and admission into
one session Location scope and extends media-blob storage. The fork retains
`command` at invocation, prompt-hook input, inbox admission, and delivery; stored
and media-view schemas inherit it too. Regression checks cover `/review` and
`/learn` admission/delivery and command metadata alongside stored attachments.

The provider restriction plugin is in `PluginInternal.guarded`, so project plugin
removal cannot disable that registration. Other plugin replacement/removal remains
part of the trust boundary: arbitrary plugins are executable code.

## Configuration semantics

The canonical public security schema is
[`packages/schema/src/config/security.ts`](packages/schema/src/config/security.ts).
Legacy normalization preserves `security`, `provenance`, provider overrides, and
`restrict_to_configured_providers`. Native V2 provider entries use `providers` and
`settings.baseURL`; legacy `provider`/`options` inputs are migrated.

- `internal-only` rejects unmatched external hosts. `strict` rejects all unmatched
  hosts. Explicit allowlists can permit external hosts in either mode.
- Localhost and private IP literals are allowed unless `allow_local` and
  `allow_private_ip` are explicitly false, including in strict mode.
- Internal DNS suffixes, CIDRs, and `external_domains` are allow rules. They are
  evaluated before `block_domain_regex`, so an earlier allow wins.
- `allow_external_ips` permits **any public IP literal** in internal-only mode;
  use `allowed_ip_ranges` for a targeted IP allowlist.
- URL checks classify the supplied hostname/IP; they do not resolve and pin DNS
  addresses. Redirects and DNS rebinding are not comprehensively controlled.
- `audit_log_enabled: false` suppresses the corresponding URL/shell audit entries;
  provenance is controlled separately.
- `models_dev_enabled: false` disables guarded catalog network refreshes. V2 has a
  bundled `models-dev/snapshot.txt` and a cache, so this does **not** empty the model
  catalog or by itself disable cloud providers.

Catalog snapshot fetching during release/build preparation is separate from
runtime refresh policy. `core/script/models-snapshot.ts` can fetch and validate a
catalog; normal local builds read the committed snapshot or an explicit
`OPENCODE_MODELS_SNAPSHOT` file. Runtime managed settings do not govern build-time
network access or determine the contents of an already bundled catalog.

For restrictive deployments, supply managed policy, explicit provider selection,
container egress controls, and sandbox configuration together. No single config
field establishes a complete network boundary.

## Shell sandbox scope

`OPENCODE_SPAWN_SANDBOX=1` (or `true`) wraps `Shell.Service` execution with `bwrap`
or, if unavailable, `unshare`, creating a user and network namespace. The wrapper
uses the original shell with a POSIX `-c` command. It is network isolation, not
filesystem isolation: the filesystem remains visible and writable according to
the process's existing permissions.

Known limits:

- If neither utility is available, the current implementation warns and runs
  **unsandboxed**. Non-Linux platforms also run unchanged. Packaging must ensure
  the utility exists and the kernel/container allows namespace creation.
- The wrapper assumes POSIX shell syntax; PowerShell is not covered by that
  assumption. Validate the selected shell in the target deployment.
- Direct `AppProcess`, Environment process, LSP, MCP, plugin, PTY, and updater
  spawns do not automatically pass through `Shell.Service`.
- Core has its own Git subprocesses; it is incorrect to assume all Git operations
  run through the model's shell tool.
- JavaScript HTTP requests in the parent process and browser/client connections
  are outside the shell namespace.
- Code written to disk is not sandboxed when something else later executes it.

Container egress rules are the backstop for uncovered parent-process and subprocess
traffic. This source review does not validate the running container's firewall.

## Content and logging limits

`Security.sanitizeForLLM` removes U+E0000–U+E007F and U+200B–U+200D/U+FEFF from
webfetch text and remote instructions. HTML-to-Markdown conversion omits comments
and active elements. These are narrow content mitigations: visible adversarial
instructions, search snippets, repository instructions, and shell output can still
influence the model. Raw HTML output is not equivalent to a sanitized DOM.

Audit JSONL is written under `Global.Path.data/audit.jsonl`. Provenance uses
`Global.Path.state/provenance/` by default. URLs, commands, previews, and errors may
contain sensitive data. Logs are ordinary writable files, not tamper-proof evidence;
retention, access control, redaction, and export signing are deployment concerns.
Treat log contents as untrusted data when asking another model to analyze them.

## Remaining outbound surfaces

The shared AI request executor is not globally wrapped with the fork URL policy.
Provider/model-specific routes, auth/OAuth, arbitrary MCP and plugin connections,
remote-access features, downloaders, and updates need their own review or an external
egress boundary. The voice and Console-search gates above check those specific
setup/query paths; they do not retrofit every existing AI request. Console
configuration polling and usage-limit reads are also direct HTTP paths outside
these URL gates.

V1 paths such as `share/share-next.ts`, `session/instruction.ts`, and
`util/fetch.ts` no longer describe the active runtime. V2 also removed the legacy
enterprise sharing package/routes. Do not use old share-disable environment
variables or v1-only flags as proof that every V2 outbound feature is disabled.

## Model routing and privacy review (2026-10-25)

This source review covers the merge of upstream `450e7b9241`. It found no evidence
of a hidden conversation-upload mechanism in the CLI/TUI paths examined. It is
not a dependency audit, a capture of a running binary's traffic, or verification
of a deployment's firewall.

There was a concrete historical routing concern: upstream
[issue #8609](https://github.com/anomalyco/opencode/issues/8609) reports V1 title
generation choosing an OpenCode-hosted model despite documentation describing a
same-provider fallback. The active V2 selection code is different:

- `session/context.ts` selects `agents.title.model` when configured; otherwise
  `Model.small` searches only the session model's provider. If no suitable smaller
  model exists, or the title attempt fails, it uses the session model. An explicit
  title model can select another provider; it can also fall back to the session
  model. Initial titles send the first user's text; regenerated titles send a
  bounded text view of the original request and recent conversation.
- `session/compaction.ts` uses the session model by default. Summary compaction
  honors `agents.compaction.model`; native compaction uses the session model and
  its provider's compaction endpoint. Compaction sends conversation content,
  including relevant tool results. Automatic background summary precomputation
  can also send content before a compaction checkpoint is installed.
- Legacy `small_model` normalizes to `agents.title.model`. Native V2 uses
  `agents`, not the legacy singular `agent` key. Setting
  `agents.title.disabled: true` removes automatic model-generated titles.
  `compaction.auto: false` disables automatic and background compaction; manual
  compaction remains available.

For an already configured, approved provider called `local`, pin all three model
selections explicitly, substituting its actual model identifier:

```jsonc
{
  "model": "local/model",
  "agents": {
    "title": { "model": "local/model" },
    "compaction": { "model": "local/model" },
  },
  "restrict_to_configured_providers": true,
}
```

Provider selection still permits configured endpoint overrides and executable
plugins. `session/model-request.ts` attaches session/project identifiers and
client/version headers to inference requests. The shared inference transport is
not covered by the fork's general URL policy, so an approved provider ID alone
does not establish an egress boundary.

Other findings relevant to confidentiality:

- OpenCode's free provider can be enabled without account credentials; model
  defaults can select its free models. `OPENCODE_DISABLE_FREE_MODELS=1` hides
  those models but is not a general network-disable switch. Upstream's
  [model privacy documentation](https://dev.opencode.ai/v2/docs/console/models#privacy)
  explicitly lists models whose request data may be used to improve the model.
  Do not infer uniform retention terms from the application name.
- A connected Console integration polls organization config and supplies
  providers, remote MCP servers, policies, and web-search selection. The new
  integration document allowlists those config keys and excludes local MCP
  commands, plugins, and credential overrides. It is still a remote trust input.
  Usage-limit requests send credentials and workspace identity, not conversation
  bodies. These account/config requests bypass the fork URL policy.
- GPT-Live can fall back to OpenCode's hosted endpoint when an OpenAI key is
  absent and an OpenCode Console key or sign-in exists. This is voice-specific,
  not a title/compaction fallback. The fork checks the actual hosted setup URL;
  allowing OpenAI's API does not implicitly allow the OpenCode host.
- CLI observability exports logs/traces only when an OTLP endpoint is configured
  (`OTEL_EXPORTER_OTLP_ENDPOINT`; see `packages/util/src/observability/otlp.ts`).
  Such exports may contain sensitive log attributes. Web/desktop builds
  initialize Sentry when `VITE_SENTRY_DSN` is set at build time; that is a separate
  outbound error-reporting path. Those clients are outside a server-only firewall.
- Model-catalog refreshes, updater checks/downloads, remote web-UI fallbacks,
  authentication, remote instructions/skills, search, MCP, and plugins have
  distinct outbound behavior. Not every outbound request contains conversation
  content, and not every path passes through the fork's URL checker.

Use egress controls around the whole application and its subprocesses, with only
approved destinations allowed. The shell network namespace does not isolate the
parent process's model, Console, or telemetry requests. Source configuration and
passing unit tests do not verify that this boundary is deployed.

## Verification and follow-up

The merge checks in [merging-upstream.md](specs/merging-upstream.md) cover the active
hooks, public config compatibility, managed precedence, instruction caching,
search handshakes, voice setup, command delivery, provider discovery, audit, and
provenance. Test policy denials by observing that the transport receives no request,
not merely by calling a URL parsing helper.

Container acceptance still needs a real namespace/egress probe, using harmless test
requests and the deployed shell, plus confirmation that logs persist outside the
analyzed workspace. Potential future work includes comprehensive Location-aware
transport policy, redirect/DNS handling, fail-closed sandbox startup, stronger
read-only execution, and log retention/redaction. These are gaps, not implemented
guarantees. Follow [the upstream playbook](specs/security-upstream-playbook.md) when
proposing security work upstream.
