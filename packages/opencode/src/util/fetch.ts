import { Effect } from "effect"
import { Config } from "@/config/config"
import { auditLogger } from "./audit"
import { validateUrlFromConfig } from "./network"

/**
 * Effect-based URL validation + audit logging.  Validates the URL against
 * SecurityConfig and logs the check.  Fails with an Error if blocked.
 * Use this to guard Effect-based HTTP pipelines (e.g. HttpClient calls).
 *
 * @param source  Short label for the caller that appears in the audit log.
 */
export function auditedUrl(url: string, source = "internal"): Effect.Effect<void, Error> {
  return Effect.promise(() => Config.get()).pipe(
    Effect.flatMap((config) => {
      if (!config.security) return Effect.void
      const result = validateUrlFromConfig(url, config.security)
      auditLogger.logUrlCheck(url, result.allowed, result.reason, { source })
      if (!result.allowed) return Effect.fail(new Error(`Fetch blocked (${result.reason}): ${url}`))
      return Effect.void
    }),
  )
}

/**
 * fetch() wrapper that validates URLs against SecurityConfig and logs all
 * requests to the audit log.  Use this in place of bare fetch() for any
 * internal HTTP call that isn't already going through webfetch/websearch/
 * provider SDK (those have their own validation and logging paths).
 *
 * @param source  Short label for the caller (e.g. "instruction", "skill-discovery")
 *                that appears in the audit log entry.
 */
export async function auditedFetch(url: string, init?: RequestInit, source = "internal"): Promise<Response> {
  const config = await Config.get()
  if (config.security) {
    const result = validateUrlFromConfig(url, config.security)
    auditLogger.logUrlCheck(url, result.allowed, result.reason, { source })
    if (!result.allowed) throw new Error(`Fetch blocked (${result.reason}): ${url}`)
  }
  const start = Date.now()
  const response = await fetch(url, init)
  auditLogger.logFetchRequest(source, init?.method ?? "GET", url, response.status, Date.now() - start)
  return response
}
