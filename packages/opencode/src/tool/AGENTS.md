## URL Validation

- `validateUrlFromConfig` only checks security ranges (localhost, private IPs, DNS suffixes) — it does not validate URL format, so callers must ensure URLs start with `http://` or `https://` first
- `String()` conversion of undefined/null produces `"undefined"`/`"null"` which would pass `validateUrlFromConfig` in external-allowed mode — the protocol check catches this
- webfetch.ts uses `redirect: "manual"` to prevent SSRF via redirect chains; each redirect target is validated against security config before following

## Command Guard (command-guard.ts)

`command-guard.ts` enforces read-only mode for `/review` and `/learn` commands. Three exported functions are called from the relevant tool implementations:

- `assertReadOnlyShell(ctx, command)` — blocks shell redirection (`>`, `>>`, `<<`, `<(`), file-mutating commands (`rm`, `mv`, `cp`, `chmod`, `chown`, `touch`, `mkdir`, `rmdir`), and mutating git subcommands (`reset`, `checkout`, `restore`, `stash`, `clean`, `rebase`, `merge`, `commit`, `push`, `cherry-pick`, `revert`, `switch`, `branch`, `tag`) for both `/review` and `/learn`
- `assertLearnPath(ctx, filePath)` — blocks all file writes in `/review`; in `/learn`, allows only `AGENTS.md` file writes
- `assertLearnTask(ctx)` — prevents `/learn` from spawning sub-agents via the task tool

The guard reads `ctx.extra?.command` to determine which slash command is active. If `ctx.extra.command` is not set (normal session), all checks are no-ops.

**Limitation**: blocklist-based approach; does not cover every file-writing utility (`tee`, `dd`, etc.). The user permission prompt displays the full command string before execution, providing a second line of defense.

## Bash Tool (bash.ts)

- **Audit logging**: every permitted bash command is logged to `audit.jsonl` via `auditLogger.logToolRequest()` AFTER both `ctx.ask()` permission checks pass — only permitted commands are logged, never blocked ones
- **Sandbox fallback**: if `OPENCODE_SPAWN_SANDBOX=1` but neither `bwrap` nor `unshare` is found, logs a warning and runs unsandboxed (no error thrown). Check logs if sandbox coverage is uncertain.
- bash.ts uses inline `sandboxedArgs()` (direct `child_process.spawn`), not `Process.spawn`. If migrating to `Process.spawn`, the sandbox wrapper in `util/process.ts` (`sandboxedCmd()`) would cover it automatically.
- `ip link set lo up` is prepended to the command inside the network namespace — required to bring loopback up so test servers on localhost are reachable within the sandbox
