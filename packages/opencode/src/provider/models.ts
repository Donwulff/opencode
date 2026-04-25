import { Global } from "@opencode-ai/core/global"
import { Log } from "../util"
import path from "path"
import { Schema } from "effect"
import { Installation } from "../installation"
import { Flag } from "@opencode-ai/core/flag/flag"
import { lazy } from "@/util/lazy"
import { Config } from "@/config"
import { auditLogger } from "@/util/audit"
import { validateUrlFromConfig } from "@/util/network"
import type { SecurityConfigType } from "@/util/network"
import { Filesystem } from "../util"
import { Flock } from "@opencode-ai/core/util/flock"
import { Hash } from "@opencode-ai/core/util/hash"

// Try to import bundled snapshot (generated at build time)
// Falls back to undefined in dev mode when snapshot doesn't exist
/* @ts-ignore */

const log = Log.create({ service: "models.dev" })
const source = url()
const filepath = path.join(
  Global.Path.cache,
  source === "https://models.dev" ? "models.json" : `models-${Hash.fast(source)}.json`,
)
const ttl = 5 * 60 * 1000

const Cost = Schema.Struct({
  input: Schema.Number,
  output: Schema.Number,
  cache_read: Schema.optional(Schema.Number),
  cache_write: Schema.optional(Schema.Number),
  context_over_200k: Schema.optional(
    Schema.Struct({
      input: Schema.Number,
      output: Schema.Number,
      cache_read: Schema.optional(Schema.Number),
      cache_write: Schema.optional(Schema.Number),
    }),
  ),
})

export const Model = Schema.Struct({
  id: Schema.String,
  name: Schema.String,
  family: Schema.optional(Schema.String),
  release_date: Schema.String,
  attachment: Schema.Boolean,
  reasoning: Schema.Boolean,
  temperature: Schema.Boolean,
  tool_call: Schema.Boolean,
  interleaved: Schema.optional(
    Schema.Union([
      Schema.Literal(true),
      Schema.Struct({
        field: Schema.Literals(["reasoning_content", "reasoning_details"]),
      }),
    ]),
  ),
  cost: Schema.optional(Cost),
  limit: Schema.Struct({
    context: Schema.Number,
    input: Schema.optional(Schema.Number),
    output: Schema.Number,
  }),
  modalities: Schema.optional(
    Schema.Struct({
      input: Schema.Array(Schema.Literals(["text", "audio", "image", "video", "pdf"])),
      output: Schema.Array(Schema.Literals(["text", "audio", "image", "video", "pdf"])),
    }),
  ),
  experimental: Schema.optional(
    Schema.Struct({
      modes: Schema.optional(
        Schema.Record(
          Schema.String,
          Schema.Struct({
            cost: Schema.optional(Cost),
            provider: Schema.optional(
              Schema.Struct({
                body: Schema.optional(Schema.Record(Schema.String, Schema.MutableJson)),
                headers: Schema.optional(Schema.Record(Schema.String, Schema.String)),
              }),
            ),
          }),
        ),
      ),
    }),
  ),
  status: Schema.optional(Schema.Literals(["alpha", "beta", "deprecated"])),
  provider: Schema.optional(
    Schema.Struct({ npm: Schema.optional(Schema.String), api: Schema.optional(Schema.String) }),
  ),
})
export type Model = Schema.Schema.Type<typeof Model>

export const Provider = Schema.Struct({
  api: Schema.optional(Schema.String),
  name: Schema.String,
  env: Schema.Array(Schema.String),
  id: Schema.String,
  npm: Schema.optional(Schema.String),
  models: Schema.Record(Schema.String, Model),
})

export type Provider = Schema.Schema.Type<typeof Provider>

function url() {
  const u = Flag.OPENCODE_MODELS_URL || "https://models.dev"
  if (!u.startsWith("http://") && !u.startsWith("https://")) {
    throw new Error("models.dev URL must start with http:// or https://")
  }
  return u
}

function fresh() {
  return Date.now() - Number(Filesystem.stat(filepath)?.mtimeMs ?? 0) < ttl
}

function skip(force: boolean) {
  return !force && fresh()
}

const fetchApi = async () => {
  const result = await fetch(`${url()}/api.json`, {
    headers: { "User-Agent": Installation.USER_AGENT },
    signal: AbortSignal.timeout(10000),
  })
  return { ok: result.ok, text: await result.text() }
}

async function check() {
  const config = await Config.get()
  const security = config.security as SecurityConfigType | undefined
  const api = `${url()}/api.json`
  if (security?.models_dev_enabled === false) {
    log.info("models.dev fetching is disabled by security configuration")
    auditLogger.logProviderLoad("models.dev", "api", 0, false, "models_dev disabled")
    return { allowed: false as const, reason: "models_dev disabled" }
  }
  if (!security) return { allowed: true as const, reason: undefined }

  const result = validateUrlFromConfig(url(), security)
  auditLogger.logUrlCheck(api, result.allowed, result.reason, {
    providerId: "models.dev",
    source: "config",
  })

  if (!result.allowed) {
    log.error("models.dev access blocked by security configuration", {
      url: url(),
      reason: result.reason,
    })
    auditLogger.logProviderLoad("models.dev", "api", 0, false, result.reason)
    return { allowed: false as const, reason: result.reason }
  }

  return { allowed: true as const, reason: result.reason }
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
  return Flock.withLock(`models-dev:${filepath}`, async () => {
    const result = await Filesystem.readJson(Flag.OPENCODE_MODELS_PATH ?? filepath).catch(() => {})
    if (result) return result
    const state = await check()
    if (!state.allowed) return {}
    const next = await fetchApi()
    if (next.ok) {
      await Filesystem.write(filepath, next.text).catch((e) => {
        log.error("Failed to write models cache", { error: e })
      })
    }
    return JSON.parse(next.text)
  })
})

export async function get() {
  const result = await Data()
  return result as Record<string, Provider>
}

export async function refresh(force = false) {
  if (skip(force)) return Data.reset()
  await Flock.withLock(`models-dev:${filepath}`, async () => {
    if (skip(force)) return Data.reset()
    const state = await check()
    if (!state.allowed) return
    const result = await fetchApi()
    if (!result.ok) return
    await Filesystem.write(filepath, result.text)
    Data.reset()
    auditLogger.logProviderLoad("models.dev", "api", 0, true, "Updated models list")
  }).catch((e) => {
    log.error("Failed to fetch models.dev", {
      error: e,
    })
    auditLogger.logProviderLoad("models.dev", "api", 0, false, `Failed to fetch: ${e}`)
  })
}

if (!Flag.OPENCODE_DISABLE_MODELS_FETCH && !process.argv.includes("--get-yargs-completions")) {
  void refresh().catch(() => {})
  setInterval(
    async () => {
      try {
        const config = await Config.get()
        const securityConfig = config.security as SecurityConfigType | undefined
        if (securityConfig?.models_dev_enabled !== false) {
          await refresh()
        }
      } catch {
        // Config not available in test environment
      }
    },
    60 * 1000 * 60,
  ).unref()
}
