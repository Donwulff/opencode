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

## Provider Loading

- CUSTOM_LOADERS dynamically load OpenAI-compatible providers; the `state()` function orchestrates full discovery
- Providers with `disable: true` are added to the `disabled` set and skipped
- GitLab instance URL validation must use `opts.instanceUrl`, not `opts.baseURL` (which is undefined in that scope)
- Llama.cpp error path uses `apiURL`, not `baseURL` (undefined in the try block scope)
- **Branded ID types**: since upstream branded `ProviderID`/`ModelID`, plain string literals
  are no longer assignable — fork additions must use `ProviderID.make("string")` and
  `ModelID.make("string")`. Any merge introducing TS2322 on a `providerID` or `id` field
  in fork-specific code (e.g. llama.cpp loader) needs this fix.
