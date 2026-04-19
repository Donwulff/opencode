# Provider Loading & Security

## Security

- URL validation occurs BEFORE fetch operations (models.ts, provider.ts getSDK wrapper)
- Security validation at both provider load time (`custom()` loaders) and runtime HTTP requests (getSDK fetch wrapper)
- Provider validation returns `disable: true` instead of throwing, so other providers can still load
- ModelsDev.Data() and ModelsDev.refresh() must have identical security validation — they are separate code paths to the same fetch
- Audit logging uses `providerId` (camelCase); data structures use `providerID` (PascalCase)
- `allow_external_ips` only applies to literal IP addresses, not DNS names (checked with `isIP()`)
- `models_dev_enabled` config flag disables models.dev auto-refresh and `Data()` lazy evaluation
- ModelsDev refresh interval is `60 * 1000 * 60` (60 min) with `.unref()` to avoid hanging the process
- **`ModelsDev.Data()` load order**: cache file → snapshot import → live fetch. The snapshot is loaded
  BEFORE the `models_dev_enabled` check is ever reached. An empty object `{}` IS truthy, so
  `if (snapshot) return snapshot` short-circuits without fetching. Blanking the snapshot in
  Dockerfile.builder (`export const snapshot = {}`) is therefore the correct way to suppress
  cloud models — `models_dev_enabled: false` alone is insufficient for a built binary.
- **`opencode` provider free-tier autoload**: unlike all other `custom()` loaders (which have
  `autoload: false`), the `opencode` provider conditionally autoloads free-tier models
  (cost.input === 0) even without an API key, using `apiKey: "public"`. With an empty snapshot
  `input.models` is `{}` so `autoload` becomes false and nothing loads — but if the snapshot
  is ever non-empty, free models appear. Belt-and-suspenders: add `"opencode"` to
  `disabled_providers` in the managed config.

## Provider Loading

- `custom()` returns named loaders for specific providers; the `state()` function orchestrates full discovery
- Providers with `disable: true` are added to the `disabled` set and skipped
- GitLab instance URL validation must use `opts.instanceUrl`, not `opts.baseURL` (which is undefined in that scope)
- **Branded ID types**: since upstream branded `ProviderID`/`ModelID`, plain string literals
  are no longer assignable — fork additions must use `ProviderID.make("string")` and
  `ModelID.make("string")`.

## Autodiscovery (fork feature)

- Config providers with `autodiscover: true` get their `/v1/models` endpoint queried at startup
- Runs after the `custom(dep)` loop and config re-apply in `state()`
- Resolves `api` or `options.baseURL`, normalizes to `/v1`, fetches `/v1/models` (10s timeout)
- Handles both `data.data` (OpenAI/vLLM standard) and `data.models` response shapes
- Reads `context_length` from response (vLLM exposes this); falls back to 128k
- Skips models already defined explicitly in config (manual overrides take precedence)
- Discovered models are hardcoded to `npm: "@ai-sdk/openai-compatible"`, `cost: 0`,
  `toolcall: true`, text-only modalities. Override by defining the model explicitly under
  `provider.<id>.models` in config.
- Registers a `languageModel` loader for autodiscovered providers
- `CustomLoader` return type includes `models` field (fork-maintained; upstream doesn't have it)
- Discovery loaders (`discoverModels` from custom loaders) are run generically for all providers,
  not just gitlab

## Config shape — where `autodiscover` goes

`autodiscover` is a **top-level** provider field, sibling to `name`/`api`/`options`. It is
**not** a member of `options`. The provider schema (`src/config/provider.ts`) is `.strict()` at
the top level but `options` uses `.catchall(z.any())`, so misplacing it under `options` is
silently accepted and becomes a no-op (discovery never fires, no error surfaces). Misspelling
the top-level key (e.g. `autodiscovery`) is caught by zod and produces a readable parse error.

Either `api` (top-level) OR `options.baseURL` satisfies the URL requirement — the resolver is
`provider.options?.baseURL ?? provider.api`. Both forms are valid; prefer the one that matches
the rest of your provider config (the AI SDK loader reads `options.baseURL`, so setting it
avoids duplication).

## Config example

```jsonc
{
  "provider": {
    "vllm-code": {
      "name": "vLLM Code Models",
      "api": "http://localhost:8001/v1",
      "autodiscover": true
    },
    "vllm-chat": {
      "name": "vLLM Chat",
      "options": { "baseURL": "http://localhost:8002/v1" },
      "autodiscover": true,
      "system_prompt": "{file:llama4.txt}"
    }
  }
}
```

Multiple providers may set `autodiscover: true` simultaneously; each is queried independently.

## Troubleshooting

- TUI shows only `UnknownError` with a mangled stack? That is `NamedError.Unknown` from
  `packages/shared/src/util/error.ts` — it wraps arbitrary throws. The real error is in the
  timestamped `.log` file under `Global.Path.log` (`~/.local/share/opencode/log/` or the
  container's mounted share dir).
- Autodiscover fetch failures are caught inside `Effect.promise` in `provider.ts` and only
  surface as `log.warn` entries (`autodiscover: failed to fetch models` / `autodiscover: error`).
  Always grep the log for `autodiscover:` when models don't appear.
- If TUI bootstrap itself dies on startup with a `{file:...}` in any provider config, the
  substitution (`src/config/variable.ts`) runs at config load and throws `InvalidError` when
  the referenced file is missing — resolved relative to the config file's directory, not CWD.
- Read-only config mounts (`OPENCODE_CONFIG_DIR` pointed at a `:ro` bind) used to crash in
  `Config.ensureGitignore` because only `PermissionDenied` was caught; EROFS fell through. The
  catch was widened to `Effect.catchAll` — this is best-effort, failing should never be fatal.
