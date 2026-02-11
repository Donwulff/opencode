# Audit & Network Utilities

## Audit Logger

- JSONL append format via `Bun.write(filePath, content, { append: true })`
- Auto-saves on each `log()` call
- Uses `providerId` (camelCase) in API methods; `getFiltered()` checks both `context` and `metadata` for this field

## Network Validation

- `validateUrlFromConfig()` explicitly blocks localhost/private IPs when the corresponding config flag is false (not just skip-allowing)
- IPv4-mapped IPv6 addresses (e.g. `::ffff:7f00:1`) are normalized to IPv4 before checks
- `isPrivateIP` covers: RFC 1918, loopback, link-local (169.254/16), carrier-grade NAT (100.64/10), and IPv6 equivalents (fe80::/10, fc00::/7)
- `isInternalDNS` enforces domain boundary — suffix `corp.com` matches `api.corp.com` but not `evil-corp.com`
