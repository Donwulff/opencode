# Test Utilities

- Audit logger API uses `providerId` (camelCase); data structures use `providerID` (PascalCase)
- Run a single test file: `bun test test/util/network.test.ts` (from packages/opencode)
- Network validation tests require full URL format coverage including edge cases like "undefined" and "null" strings
