export * as AutodiscoverPlugin from "./autodiscover.js"

import { define } from "@opencode/plugin/effect/plugin"
import { Model } from "@opencode/schema/model"
import { Provider } from "../../provider.js"
import { Effect, Schema, Stream } from "effect"
import { HttpClient, HttpClientRequest, HttpClientResponse } from "effect/http"
import { Config } from "../../config.js"
import { Security } from "../../util/security.js"

const Item = Schema.Struct({
  id: Schema.optional(Schema.String),
  name: Schema.optional(Schema.String),
  context_length: Schema.optional(Schema.Int),
  max_model_len: Schema.optional(Schema.Int),
})
const Response = Schema.Struct({
  data: Schema.optional(Schema.Array(Item)),
  models: Schema.optional(Schema.Array(Item)),
})

export const Plugin = define({
  id: "opencode.provider.autodiscover",
  effect: Effect.fn(function* (ctx) {
    const config = yield* Config.Service
    const http = yield* HttpClient.HttpClient
    const discovered = new Map<string, { baseURL: string; models: Model.Info[] }>()
    const load = Effect.fn("AutodiscoverPlugin.load")(function* () {
      discovered.clear()
      const entries = yield* config.entries()
      const configured = new Map(
        entries.flatMap((entry) => (entry.type === "document" ? Object.entries(entry.info.providers ?? {}) : [])),
      )
      for (const [providerID, provider] of configured) {
        if (!provider.autodiscover) continue
        const url = provider.settings?.baseURL
        if (typeof url !== "string") continue
        const trimmed = url.replace(/\/+$/, "")
        const baseURL = trimmed.endsWith("/v1") ? trimmed : `${trimmed}/v1`
        yield* Effect.gen(function* () {
          yield* Security.assertUrl(config, `${baseURL}/models`, "provider-autodiscover")
          const response = yield* HttpClientRequest.get(`${baseURL}/models`).pipe(
            http.execute,
            Effect.flatMap(HttpClientResponse.schemaBodyJson(Response)),
            Effect.timeout("10 seconds"),
          )
          const models = (response.data ?? response.models ?? []).flatMap((item) => {
            const id = item.id ?? item.name
            if (!id) return []
            const context = item.context_length ?? item.max_model_len ?? 128_000
            return [
              {
                ...Model.Info.default(Provider.ID.make(providerID), Model.ID.make(id)),
                name: item.name ?? id,
                settings: { baseURL },
                capabilities: { tools: true, input: ["text"], output: ["text"] },
                limit: { context, output: Math.min(context, 16_384) },
              },
            ]
          })
          discovered.set(providerID, { baseURL, models })
        }).pipe(Effect.catch((error) => Effect.logWarning("provider autodiscovery failed", { providerID, error })))
      }
    })
    yield* ctx.provider.transform((providers) => {
      for (const [id, source] of discovered) {
        const existing = providers.get(id)
        providers.update(id, (provider) => {
          provider.activation = "enabled"
          provider.settings = { ...provider.settings, baseURL: source.baseURL }
          if (!existing) provider.package = Provider.aisdk("@ai-sdk/openai-compatible")
        })
        for (const model of source.models) {
          if (existing?.models.has(model.id)) continue
          providers.models.update(id, model.id, (target) => Object.assign(target, model))
        }
      }
    })
    yield* ctx.event.subscribe().pipe(
      Stream.filter((event) => event.type === "config.updated"),
      Stream.runForEach(() => load().pipe(Effect.andThen(ctx.provider.reload()))),
      Effect.forkScoped({ startImmediately: true }),
    )
    yield* load()
    yield* ctx.provider.reload()
  }),
})
