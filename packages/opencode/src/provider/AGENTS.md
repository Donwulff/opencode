# Provider Loading & Security

## Security

- URL validation occurs BEFORE fetch operations (models.ts, provider.ts getSDK wrapper)
- Security validation at both provider load time (CUSTOM_LOADERS) and runtime HTTP requests (getSDK fetch wrapper)
- Provider validation returns `disable: true` instead of throwing, so other providers can still load
- ModelsDev.Data() and ModelsDev.refresh() must have identical security validation — they are separate code paths to the same fetch
- Audit logging uses `providerId` (camelCase); data structures use `providerID` (PascalCase)
- `allow_external_ips` only applies to literal IP addresses, not DNS names (checked with `isIP()`)

## Provider Loading

- CUSTOM_LOADERS dynamically load OpenAI-compatible providers; the `state()` function orchestrates full discovery
- Providers with `disable: true` are added to the `disabled` set and skipped
- GitLab instance URL validation must use `opts.instanceUrl`, not `opts.baseURL` (which is undefined in that scope)
- Llama.cpp error path uses `apiURL`, not `baseURL` (undefined in the try block scope)
