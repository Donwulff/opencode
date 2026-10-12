export * as RestrictProvidersPlugin from "./restrict.js"

import { define } from "@opencode/plugin/effect/plugin"
import { Effect } from "effect"
import { Config } from "../../config.js"
import { ConfigEntryObserver } from "../../config/plugin/entry-observer.js"
import { validateUrlFromConfig } from "../../util/network.js"

export const Plugin = define({
  id: "opencode.provider.restrict",
  effect: Effect.fn(function* (ctx) {
    const config = yield* Config.Service
    const loaded = yield* ConfigEntryObserver.observe(config, ctx.event, ctx.provider.reload())
    yield* ctx.provider.transform((providers) => {
      const environment = process.env.OPENCODE_RESTRICT_PROVIDERS?.toLowerCase()
      const restrict =
        Config.latest(loaded.entries, "restrict_to_configured_providers") ??
        (environment === "1" || environment === "true")
      const security = Config.latest(loaded.entries, "security")
      const configured = new Set(
        loaded.entries.flatMap((entry) => (entry.type === "document" ? Object.keys(entry.info.providers ?? {}) : [])),
      )
      for (const record of providers.list()) {
        const url = record.provider.settings?.baseURL
        if (
          (!restrict || configured.has(record.provider.id)) &&
          (!security || typeof url !== "string" || validateUrlFromConfig(url, security).allowed)
        )
          continue
        providers.update(record.provider.id, (provider) => {
          provider.activation = "disabled"
        })
      }
    })
  }),
})
