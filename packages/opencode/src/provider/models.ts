import { Global } from "../global"
import { Log } from "../util/log"
import path from "path"
import z from "zod"
import { Installation } from "../installation"
import { Flag } from "../flag/flag"
import { lazy } from "@/util/lazy"
import { Config } from "@/config/config"
import { auditLogger } from "@/util/audit"
import { validateUrlFromConfig } from "@/util/network"
import type { SecurityConfigType } from "@/util/network"
import { Filesystem } from "../util/filesystem"

// Try to import bundled snapshot (generated at build time)
// Falls back to undefined in dev mode when snapshot doesn't exist
/* @ts-ignore */

export namespace ModelsDev {
  const log = Log.create({ service: "models.dev" })
  const filepath = path.join(Global.Path.cache, "models.json")

  export const Model = z.object({
    id: z.string(),
    name: z.string(),
    family: z.string().optional(),
    release_date: z.string(),
    attachment: z.boolean(),
    reasoning: z.boolean(),
    temperature: z.boolean(),
    tool_call: z.boolean(),
    interleaved: z
      .union([
        z.literal(true),
        z
          .object({
            field: z.enum(["reasoning_content", "reasoning_details"]),
          })
          .strict(),
      ])
      .optional(),
    cost: z
      .object({
        input: z.number(),
        output: z.number(),
        cache_read: z.number().optional(),
        cache_write: z.number().optional(),
        context_over_200k: z
          .object({
            input: z.number(),
            output: z.number(),
            cache_read: z.number().optional(),
            cache_write: z.number().optional(),
          })
          .optional(),
      })
      .optional(),
    limit: z.object({
      context: z.number(),
      input: z.number().optional(),
      output: z.number(),
    }),
    modalities: z
      .object({
        input: z.array(z.enum(["text", "audio", "image", "video", "pdf"])),
        output: z.array(z.enum(["text", "audio", "image", "video", "pdf"])),
      })
      .optional(),
    experimental: z.boolean().optional(),
    status: z.enum(["alpha", "beta", "deprecated"]).optional(),
    options: z.record(z.string(), z.any()),
    headers: z.record(z.string(), z.string()).optional(),
    provider: z.object({ npm: z.string().optional(), api: z.string().optional() }).optional(),
    variants: z.record(z.string(), z.record(z.string(), z.any())).optional(),
  })
  export type Model = z.infer<typeof Model>

  export const Provider = z.object({
    api: z.string().optional(),
    name: z.string(),
    env: z.array(z.string()),
    id: z.string(),
    npm: z.string().optional(),
    models: z.record(z.string(), Model),
  })

  export type Provider = z.infer<typeof Provider>

  function url() {
    const u = Flag.OPENCODE_MODELS_URL || "https://models.dev"
    if (!u.startsWith("http://") && !u.startsWith("https://")) {
      throw new Error("models.dev URL must start with http:// or https://")
    }
    return u
  }

  export const Data = lazy(async () => {
    const result = await Filesystem.readJson(Flag.OPENCODE_MODELS_PATH ?? filepath).catch(() => {})
    if (result) return result
    // @ts-ignore
    const snapshot = await import("./models-snapshot.js")
      .then((m) => m.snapshot as Record<string, unknown>)
      .catch(() => undefined)
    if (snapshot) return snapshot
    if (Flag.OPENCODE_DISABLE_MODELS_FETCH) return {}

    const config = await Config.get()
    const securityConfig = config.security as SecurityConfigType | undefined
    const modelsUrl = `${url()}/api.json`

    if (securityConfig?.models_dev_enabled === false) {
      log.info("models.dev fetching is disabled by security configuration")
      auditLogger.logProviderLoad("models.dev", "api", 0, false, "models_dev disabled")
      return {}
    }

    if (securityConfig) {
      const validation = validateUrlFromConfig(url(), securityConfig)
      if (!validation.allowed) {
        log.error("models.dev access blocked by security configuration", {
          url: url(),
          reason: validation.reason,
        })
        auditLogger.logUrlCheck(modelsUrl, false, validation.reason, {
          providerId: "models.dev",
          source: "config",
        })

        if (securityConfig.mode === "strict") {
          auditLogger.logProviderLoad("models.dev", "api", 0, false, validation.reason)
          return {}
        }
        return {}
      }
      auditLogger.logUrlCheck(modelsUrl, true, validation.reason, {
        providerId: "models.dev",
        source: "config",
      })
    }

    const json = await fetch(modelsUrl).then((x) => x.text())
    return JSON.parse(json)
  })

  export async function get() {
    const result = await Data()
    return result as Record<string, Provider>
  }

  export async function refresh() {
    const config = await Config.get()
    const securityConfig = config.security as SecurityConfigType | undefined
    const modelsUrl = `${url()}/api.json`

    // Check if models_dev is enabled
    if (securityConfig?.models_dev_enabled === false) {
      log.info("models.dev fetching is disabled by security configuration")
      auditLogger.logProviderLoad("models.dev", "api", 0, false, "models_dev disabled")
      return
    }

    // Validate URL before fetch
    if (securityConfig && !Flag.OPENCODE_DISABLE_MODELS_FETCH) {
      const validation = validateUrlFromConfig(url(), securityConfig)
      if (!validation.allowed) {
        log.error("models.dev access blocked by security configuration", {
          url: url(),
          reason: validation.reason,
        })
        auditLogger.logUrlCheck(modelsUrl, false, validation.reason, {
          providerId: "models.dev",
          source: "config",
        })

        if (securityConfig.mode === "strict") {
          auditLogger.logProviderLoad("models.dev", "api", 0, false, validation.reason)
          return
        }
        return
      }
      auditLogger.logUrlCheck(modelsUrl, true, validation.reason, {
        providerId: "models.dev",
        source: "config",
      })
    }

    const result = await fetch(modelsUrl, {
      headers: {
        "User-Agent": Installation.USER_AGENT,
      },
      signal: AbortSignal.timeout(10 * 1000),
    }).catch((e) => {
      log.error("Failed to fetch models.dev", {
        error: e,
      })
      auditLogger.logProviderLoad("models.dev", "api", 0, false, `Failed to fetch: ${e}`)
    })

    if (result && result.ok) {
      await Filesystem.write(filepath, await result.text())
      ModelsDev.Data.reset()
      auditLogger.logProviderLoad("models.dev", "api", 0, true, "Updated models list")
    }
  }
}

if (!Flag.OPENCODE_DISABLE_MODELS_FETCH && !process.argv.includes("--get-yargs-completions")) {
  ModelsDev.refresh().catch(() => {})
  setInterval(
    async () => {
      try {
        const config = await Config.get()
        const securityConfig = config.security as SecurityConfigType | undefined
        if (securityConfig?.models_dev_enabled !== false) {
          await ModelsDev.refresh()
        }
      } catch {
        // Config not available in test environment
      }
    },
    60 * 1000 * 60,
  ).unref()
}
