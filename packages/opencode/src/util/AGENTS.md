# Audit & Network Utilities

## Audit Logger

- JSONL append format via `Bun.write(filePath, content, { append: true })`
- Auto-saves on each `log()` call
- Uses `providerId` (camelCase) in API methods; `getFiltered()` checks both `context` and `metadata` for this field
- **Circular dependency constraint**: `config.ts` imports `audit.ts`. Do NOT put anything that also imports `config` into `audit.ts`. `util/fetch.ts` is the correct home for code that needs both config + audit (e.g. `auditedFetch()`).

## Network Validation

- `validateUrlFromConfig()` explicitly blocks localhost/private IPs when the corresponding config flag is false (not just skip-allowing)
- IPv4-mapped IPv6 addresses (e.g. `::ffff:7f00:1`) are normalized to IPv4 before checks
- `isPrivateIP` covers: RFC 1918, loopback, link-local (169.254/16), carrier-grade NAT (100.64/10), and IPv6 equivalents (fe80::/10, fc00::/7)
- `isInternalDNS` enforces domain boundary — suffix `corp.com` matches `api.corp.com` but not `evil-corp.com`

## auditedFetch (util/fetch.ts)

- Validates URL against `SecurityConfig`, logs the check, throws on blocked URLs, then logs the response
- Used by: `instruction.ts` (remote instruction URLs), `skill/discovery.ts` (skill index fetches)
- Lives in `fetch.ts` (not `audit.ts`) because of the circular dep constraint above

## Content Sanitization (tool/webfetch.ts)

- `sanitizeForLLM()` strips Unicode tag block (U+E0000–E007F) and zero-width chars (U+200B–200D, FEFF) — invisible steganographic injection vectors
- Applied to ALL non-HTML output paths; for HTML paths, HTML comments are stripped BEFORE `TurndownService` runs (TurndownService does not strip comments)
- Both are applied before content enters the LLM context — if adding new content ingestion paths, apply `sanitizeForLLM()` to the output
