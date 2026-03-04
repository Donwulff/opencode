import { Config } from "@/config/config"
import { auditLogger } from "./audit"
import { validateUrlFromConfig } from "./network"

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
