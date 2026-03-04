## URL Validation

- `validateUrlFromConfig` only checks security ranges (localhost, private IPs, DNS suffixes) — it does not validate URL format, so callers must ensure URLs start with `http://` or `https://` first
- `String()` conversion of undefined/null produces `"undefined"`/`"null"` which would pass `validateUrlFromConfig` in external-allowed mode — the protocol check catches this
- webfetch.ts uses `redirect: "manual"` to prevent SSRF via redirect chains; each redirect target is validated against security config before following

## Bash Tool (bash.ts)

- **Audit logging**: every permitted bash command is logged to `audit.jsonl` via `auditLogger.logToolRequest()` AFTER both `ctx.ask()` permission checks pass — only permitted commands are logged, never blocked ones
- **Sandbox fallback**: if `OPENCODE_SPAWN_SANDBOX=1` but neither `bwrap` nor `unshare` is found, logs a warning and runs unsandboxed (no error thrown). Check logs if sandbox coverage is uncertain.
- bash.ts uses inline `sandboxedArgs()` (direct `child_process.spawn`), not `Process.spawn`. If migrating to `Process.spawn`, the sandbox wrapper in `util/process.ts` (`sandboxedCmd()`) would cover it automatically.
- `ip link set lo up` is prepended to the command inside the network namespace — required to bring loopback up so test servers on localhost are reachable within the sandbox
