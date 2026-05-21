## Webfetch Tool (webfetch.ts)

- `htmlparser2` `Parser` is fully synchronous — `write()` + `end()` + result access all happen in the same tick. Do NOT wrap `extractTextFromHTML()` in `async`/`Effect.promise`; call it directly.

## URL Validation

- `validateUrlFromConfig` only checks security ranges (localhost, private IPs, DNS suffixes) — it does not validate URL format, so callers must ensure URLs start with `http://` or `https://` first
- `String()` conversion of undefined/null produces `"undefined"`/`"null"` which would pass `validateUrlFromConfig` in external-allowed mode — the protocol check catches this

## Command Guard (command-guard.ts)

`command-guard.ts` enforces read-only mode for `/review` and `/learn` commands. Three exported functions are called from the relevant tool implementations:

- `assertReadOnlyShell(ctx, command)` — blocks shell redirection (`>`, `>>`, `<<`, `<(`), file-mutating commands (`rm`, `mv`, `cp`, `chmod`, `chown`, `touch`, `mkdir`, `rmdir`), and mutating git subcommands (`reset`, `checkout`, `restore`, `stash`, `clean`, `rebase`, `merge`, `commit`, `push`, `cherry-pick`, `revert`, `switch`, `branch`, `tag`) for both `/review` and `/learn`
- `assertLearnPath(ctx, filePath)` — blocks all file writes in `/review`; in `/learn`, allows only `AGENTS.md` file writes
- `assertLearnTask(ctx)` — prevents `/learn` from spawning sub-agents via the task tool

The guard reads `ctx.extra?.command` to determine which slash command is active. If `ctx.extra.command` is not set (normal session), all checks are no-ops.

**Limitation**: blocklist-based approach; does not cover every file-writing utility (`tee`, `dd`, etc.). The user permission prompt displays the full command string before execution, providing a second line of defense.

## Shell Tool (shell.ts)

Renamed from `bash.ts` in the 2026-05-03 upstream merge. ToolID is still `"bash"` for plugin/permission compatibility — see `tool/shell/id.ts`.

- **Audit logging**: every permitted shell command is logged to `audit.jsonl` via `auditLogger.logToolRequest()` AFTER `yield* ask(ctx, scan)` permission check passes — only permitted commands are logged, never blocked ones
- **Sandbox fallback**: if `OPENCODE_SPAWN_SANDBOX=1` but neither `bwrap` nor `unshare` is found, logs a warning and runs unsandboxed (no error thrown). Check logs if sandbox coverage is uncertain.
- shell.ts uses inline `sandboxedArgs()` with Effect `ChildProcessSpawner`; it does NOT route through `util/process.ts` `sandboxedCmd()`. Both helpers wrap commands via the same bwrap/unshare invocation; future tools that spawn subprocesses should use shell.ts's inline helper or `sandboxedCmd()` from util/process.ts.
- `ip link set lo up` is prepended to the command inside the network namespace — required to bring loopback up so test servers on localhost are reachable within the sandbox
