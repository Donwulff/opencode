import { expect } from "bun:test"
import { Document, Info } from "@opencode/schema/config"
import { Config } from "@opencode/core/config"
import { Plugin } from "@opencode/core/plugin"
import { PluginHost } from "@opencode/core/plugin/host"
import { RestrictProvidersPlugin } from "@opencode/core/plugin/provider/restrict"
import { Provider } from "@opencode/core/provider"
import { Effect, Schema } from "effect"
import { testEffect } from "../lib/effect"
import { PluginTestLayer } from "./fixture"

const it = testEffect(PluginTestLayer)

it.effect("filters the actual provider registry using configured identities and endpoint policy", () =>
  Effect.gen(function* () {
    const providers = yield* Provider.Service
    yield* providers.transform((editor) => {
      for (const [id, url] of [
        ["local", "http://localhost/v1"],
        ["external", "https://example.com/v1"],
        ["unconfigured", "http://localhost/v1"],
      ])
        editor.update(Provider.ID.make(id), (provider) => {
          provider.activation = "enabled"
          provider.settings = { baseURL: url }
        })
    })
    const plugin = yield* Plugin.Service
    const host = yield* PluginHost.make(plugin)
    yield* RestrictProvidersPlugin.Plugin.effect(host).pipe(
      Effect.provide(
        Config.testLayer([
          new Document({
            type: "document",
            info: Schema.decodeUnknownSync(Info)({
              restrict_to_configured_providers: true,
              security: { mode: "internal-only", allow_local: true },
              providers: { local: {}, external: {} },
            }),
          }),
        ]),
      ),
    )
    expect((yield* providers.available()).map((provider) => provider.id)).toEqual([Provider.ID.make("local")])
  }),
)
