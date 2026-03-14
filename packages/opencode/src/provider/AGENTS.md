# Provider Loading & Security

## Security

- URL validation occurs BEFORE fetch operations (models.ts, provider.ts getSDK wrapper)
- Security validation at both provider load time (CUSTOM_LOADERS) and runtime HTTP requests (getSDK fetch wrapper)
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
- **`opencode` provider free-tier autoload**: unlike all other CUSTOM_LOADERS (which have
  `autoload: false`), the `opencode` provider conditionally autoloads free-tier models
  (cost.input === 0) even without an API key, using `apiKey: "public"`. With an empty snapshot
  `input.models` is `{}` so `autoload` becomes false and nothing loads — but if the snapshot
  is ever non-empty, free models appear. Belt-and-suspenders: add `"opencode"` to
  `disabled_providers` in the managed config.

## Provider Loading

- CUSTOM_LOADERS dynamically load OpenAI-compatible providers; the `state()` function orchestrates full discovery
- Providers with `disable: true` are added to the `disabled` set and skipped
- GitLab instance URL validation must use `opts.instanceUrl`, not `opts.baseURL` (which is undefined in that scope)
- Llama.cpp error path uses `apiURL`, not `baseURL` (undefined in the try block scope)
- **Branded ID types**: since upstream branded `ProviderID`/`ModelID`, plain string literals
  are no longer assignable — fork additions must use `ProviderID.make("string")` and
  `ModelID.make("string")`. Any merge introducing TS2322 on a `providerID` or `id` field
  in fork-specific code (e.g. llama.cpp loader) needs this fix.
