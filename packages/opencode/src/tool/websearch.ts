import { Effect, Schema } from "effect"
import { HttpClient } from "effect/unstable/http"
import * as Tool from "./tool"
import * as McpExa from "./mcp-exa"
import DESCRIPTION from "./websearch.txt"
import { auditLogger } from "@/util/audit"
import { validateUrlFromConfig } from "@/util/network"
import type { SecurityConfigType } from "@/util/network"
import { Config } from "@/config"

export const Parameters = Schema.Struct({
  query: Schema.String.annotate({ description: "Websearch query" }),
  numResults: Schema.optional(Schema.Number).annotate({
    description: "Number of search results to return (default: 8)",
  }),
  livecrawl: Schema.optional(Schema.Literals(["fallback", "preferred"])).annotate({
    description:
      "Live crawl mode - 'fallback': use live crawling as backup if cached content unavailable, 'preferred': prioritize live crawling (default: 'fallback')",
  }),
  type: Schema.optional(Schema.Literals(["auto", "fast", "deep"])).annotate({
    description: "Search type - 'auto': balanced search (default), 'fast': quick results, 'deep': comprehensive search",
  }),
  contextMaxCharacters: Schema.optional(Schema.Number).annotate({
    description: "Maximum characters for context string optimized for LLMs (default: 10000)",
  }),
})

export const WebSearchTool = Tool.define(
  "websearch",
  Effect.gen(function* () {
    const http = yield* HttpClient.HttpClient

    return {
      get description() {
        return DESCRIPTION.replace("{{year}}", new Date().getFullYear().toString())
      },
      parameters: Parameters,
      execute: (params: Schema.Schema.Type<typeof Parameters>, ctx: Tool.Context) =>
        Effect.gen(function* () {
          yield* ctx.ask({
            permission: "websearch",
            patterns: [params.query],
            always: ["*"],
            metadata: {
              query: params.query,
              numResults: params.numResults,
              livecrawl: params.livecrawl,
              type: params.type,
              contextMaxCharacters: params.contextMaxCharacters,
            },
          })

          // Validate search endpoint against security config
          const cfg = yield* Effect.promise(() => Config.get())
          const securityConfig = cfg.security as SecurityConfigType | undefined
          if (securityConfig) {
            const mcpUrl = "https://mcp.exa.ai/mcp"
            const validation = validateUrlFromConfig(mcpUrl, securityConfig)
            if (!validation.allowed) {
              auditLogger.logToolRequest(ctx.sessionID, "websearch", mcpUrl, "unknown", false, validation.reason)
              throw new Error(validation.reason)
            }
            auditLogger.logToolRequest(ctx.sessionID, "websearch", mcpUrl, "unknown", true, "MCP endpoint validated")
          }

          const result = yield* McpExa.call(
            http,
            "web_search_exa",
            McpExa.SearchArgs,
            {
              query: params.query,
              type: params.type || "auto",
              numResults: params.numResults || 8,
              livecrawl: params.livecrawl || "fallback",
              contextMaxCharacters: params.contextMaxCharacters,
            },
            "25 seconds",
          )

          return {
            output: result ?? "No search results found. Please try a different query.",
            title: `Web search: ${params.query}`,
            metadata: {},
          }
        }).pipe(Effect.orDie),
    }
  }),
)
