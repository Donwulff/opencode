export * as Security from "./security.js"

import { Effect } from "effect"
import { Config } from "../config.js"
import { auditLogger } from "./audit.js"
import { validateUrlFromConfig } from "./network.js"

export const assertUrl = Effect.fn("Security.assertUrl")(function* (
  config: Config.Interface | undefined,
  url: string,
  source: string,
) {
  if (!config) return
  const security = Config.latest(yield* config.entries(), "security")
  if (!security) return
  const result = validateUrlFromConfig(url, security)
  if (security.audit_log_enabled !== false) auditLogger.logUrlCheck(url, result.allowed, result.reason, { source })
  if (!result.allowed) return yield* Effect.fail(new Error(`Fetch blocked (${result.reason}): ${url}`))
})

export const sanitizeForLLM = (text: string) =>
  text.replace(/[\u{E0000}-\u{E007F}]/gu, "").replace(/[\u200B-\u200D\uFEFF]/g, "")
