import { expect } from "bun:test"
import { Document, Info } from "@opencode/schema/config"
import { Config } from "@opencode/core/config"
import { Model } from "@opencode/core/model"
import { Plugin } from "@opencode/core/plugin"
import { PluginHost } from "@opencode/core/plugin/host"
import { AutodiscoverPlugin } from "@opencode/core/plugin/provider/autodiscover"
import { Provider } from "@opencode/core/provider"
import { Effect, Schema } from "effect"
import { testEffect } from "../lib/effect"
import { PluginTestLayer } from "./fixture"

const it = testEffect(PluginTestLayer)

it.live("discovers models from the configured endpoint and applies network policy before requesting", () =>
  Effect.gen(function* () {
    const requests: string[] = []
    const server = yield* Effect.acquireRelease(
      Effect.sync(() =>
        Bun.serve({
          hostname: "127.0.0.1",
          port: 0,
          fetch: (request) => {
            requests.push(new URL(request.url).pathname)
            return Response.json({ data: [{ id: "local-model", context_length: 65_536 }] })
          },
        }),
      ),
      (server) => Effect.promise(() => server.stop(true)),
    )
    const plugin = yield* Plugin.Service
    const host = yield* PluginHost.make(plugin)
    const add = (allow: boolean) =>
      AutodiscoverPlugin.Plugin.effect(host).pipe(
        Effect.provide(
          Config.testLayer([
            new Document({
              type: "document",
              info: Schema.decodeUnknownSync(Info)({
                security: { allow_local: allow, audit_log_enabled: false },
                providers: { local: { autodiscover: true, settings: { baseURL: server.url.origin } } },
              }),
            }),
          ]),
        ),
      )
    yield* add(false)
    expect(requests).toEqual([])
    yield* add(true)
    const models = yield* Model.Service
    const model = yield* models.get(Provider.ID.make("local"), Model.ID.make("local-model"))
    expect(requests).toEqual(["/v1/models"])
    expect(model).toMatchObject({
      limit: { context: 65_536, output: 16_384 },
      settings: { baseURL: `${server.url.origin}/v1` },
    })
  }),
)
