# Config

- `security.audit_log_enabled` must be wired explicitly via `setAuditLogEnabled()` in config.ts after config merge — it is not automatic
- Managed config (enterprise) has highest priority but only applies to admin settings, not skills/plugins/commands
- **Config precedence (low → high)**: defaults → `~/.config/opencode/` (user global) → `.opencode/` (project) → `$OPENCODE_CONFIG_DIR` → `$OPENCODE_CONFIG_CONTENT` (inline) → `/etc/opencode/` (managed/enterprise — highest)
- **`OPENCODE_TEST_MANAGED_CONFIG_DIR`**: env var that redirects the managed config path lookup (`config.ts:60`). Set to a non-existent path to bypass `/etc/opencode/` for testing or for `--dangerously-open` mode. This is the only way to override managed config at runtime since it has higher precedence than `OPENCODE_CONFIG_DIR`.
