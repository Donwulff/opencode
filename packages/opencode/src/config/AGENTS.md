# Config

- `security.audit_log_enabled` must be wired explicitly via `setAuditLogEnabled()` in config.ts after config merge — it is not automatic
- Managed config (enterprise) has highest priority but only applies to admin settings, not skills/plugins/commands
