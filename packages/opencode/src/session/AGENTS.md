# Session

## Branded ID Types

`MessageID`, `PartID`, `SessionID` are branded types — plain strings are not assignable.
Always use the typed constructors from `./schema`:

```typescript
MessageID.ascending()      // new unique ascending ID
PartID.ascending()         // new unique ascending ID
MessageID.make("str")      // wrap a known string as MessageID
```

Do NOT use `Identifier.ascending("message")` directly — returns plain `string`, not branded
`MessageID`. The session schema wrappers are the correct path and are already imported in
most session files.

This comes up in fork-specific prompt paths (e.g. plan-mode assistant message construction).
Any TS2322 on an `id` field typed as `MessageID`/`PartID` in session code needs this fix.

## build.ts migration bundling

`packages/opencode/script/build.ts` must include a `name` field in every migration entry
it bundles into `OPENCODE_MIGRATIONS`. The `db.ts` Journal type is `{ sql, timestamp, name }`.
If `name` is missing, drizzle generates malformed SQL: `values(?, ?, , ?)`.
Dev mode (reads files directly) works fine; only the built binary is affected.
