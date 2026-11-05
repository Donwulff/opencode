export * as WebSearchFirecrawl from "./firecrawl.js"

import { define } from "@opencode/plugin/effect/plugin"
import { Effect, Option, Schema, Scope } from "effect"
import { Config } from "../../config.js"
import { Security } from "../../util/security.js"
import { HttpClient } from "effect/http"
import { App } from "../../app.js"
import { WebSearchMcp } from "./mcp.js"
import { WebSearchProvider } from "./provider.js"

export const endpoint = "https://mcp.firecrawl.dev/v2/mcp"

const McpInput = Schema.Struct({
  query: Schema.String,
  limit: Schema.Number.pipe(Schema.optional),
})

const McpOutput = Schema.Struct({
  content: Schema.Array(Schema.Struct({ type: Schema.Literal("text"), text: Schema.String })),
})

const SearchResponse = Schema.fromJsonString(
  Schema.Struct({
    success: Schema.Boolean,
    data: Schema.Struct({
      web: Schema.Array(
        Schema.Struct({
          url: Schema.String,
          title: Schema.NullOr(Schema.String).pipe(Schema.optional),
          description: Schema.NullOr(Schema.String).pipe(Schema.optional),
        }),
      ),
    }),
  }),
)
const decodeSearchResponse = Schema.decodeUnknownOption(SearchResponse)

export const Plugin = define<HttpClient.HttpClient | Scope.Scope>({
  id: "opencode.websearch.firecrawl",
  effect: Effect.fn("WebSearchFirecrawl.Plugin")(function* (ctx) {
    const http = yield* HttpClient.HttpClient
    const config = Option.getOrUndefined(yield* Effect.serviceOption(Config.Service))
    yield* WebSearchProvider.register(ctx, {
      id: "firecrawl",
      name: "Firecrawl",
      env: "FIRECRAWL_API_KEY",
      execute: (input, credential) =>
        Effect.gen(function* () {
          yield* Security.assertUrl(config, endpoint, "websearch")
          const result = yield* WebSearchMcp.call(
            http,
            endpoint,
            "firecrawl_search",
            { input: McpInput, output: McpOutput },
            { query: input.query, limit: 8 },
            {
              "User-Agent": App.useragent(ctx.app),
              ...(credential?.type === "key" ? { Authorization: `Bearer ${credential.key}` } : {}),
            },
          )
          const content = result?.content.find((item) => item.text)
          const response = content ? Option.getOrUndefined(decodeSearchResponse(content.text)) : undefined
          return (
            response?.data.web.map((item) => ({
              url: item.url,
              ...(item.title ? { title: item.title } : {}),
              ...(item.description ? { content: item.description } : {}),
              time: {},
            })) ?? []
          )
        }),
    })
  }),
})
