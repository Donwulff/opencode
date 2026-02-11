## URL Validation

- `validateUrlFromConfig` only checks security ranges (localhost, private IPs, DNS suffixes) — it does not validate URL format, so callers must ensure URLs start with `http://` or `https://` first
- `String()` conversion of undefined/null produces `"undefined"`/`"null"` which would pass `validateUrlFromConfig` in external-allowed mode — the protocol check catches this
- webfetch.ts uses `redirect: "manual"` to prevent SSRF via redirect chains; each redirect target is validated against security config before following
