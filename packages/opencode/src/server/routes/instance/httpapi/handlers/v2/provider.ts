import { Catalog } from "@opencode-ai/core/catalog"
import { PluginBoot } from "@opencode-ai/core/plugin/boot"
import type { ProviderV2 } from "@opencode-ai/core/provider"
import { Config } from "@/config/config"
import { Flag } from "@opencode-ai/core/flag/flag"
import { validateUrlFromConfig, type SecurityConfigType } from "@/util/network"
import { Effect } from "effect"
import { HttpApiBuilder } from "effect/unstable/httpapi"
import { InstanceHttpApi } from "../../api"
import { ProviderNotFoundError, ServiceUnavailableError } from "../../errors"

const catalogUnavailable = new ServiceUnavailableError({
  message: "Provider catalog is unavailable",
  service: "catalog",
})

function providerEndpointUrl(provider: ProviderV2.Info): string | undefined {
  const ep = provider.endpoint
  if (ep.type === "unknown") return undefined
  return ep.url ?? undefined
}

function applyProviderRestrictions(
  providers: ProviderV2.Info[],
  cfg: Config.Info,
): ProviderV2.Info[] {
  let result = providers

  const restrictToConfigured = cfg.restrict_to_configured_providers ?? Flag.OPENCODE_RESTRICT_PROVIDERS
  if (restrictToConfigured) {
    const configuredSet = new Set(Object.keys(cfg.provider ?? {}))
    result = result.filter((p) => configuredSet.has(p.id))
  }

  const security = cfg.security as SecurityConfigType | undefined
  if (security) {
    result = result.filter((p) => {
      const url = providerEndpointUrl(p)
      if (!url) return true
      return validateUrlFromConfig(url, security).allowed
    })
  }

  return result
}

export const providerHandlers = HttpApiBuilder.group(InstanceHttpApi, "v2.provider", (handlers) =>
  Effect.gen(function* () {
    return handlers
      .handle(
        "providers",
        Effect.fn(function* () {
          const catalog = yield* Catalog.Service
          const pluginBoot = yield* PluginBoot.Service
          yield* pluginBoot.wait().pipe(Effect.catchDefect(() => Effect.fail(catalogUnavailable)))
          let providers = yield* catalog.provider.available()
          if (Flag.OPENCODE_RESTRICT_PROVIDERS) {
            const cfg = yield* Effect.promise(() => Config.get())
            providers = applyProviderRestrictions(providers, cfg)
          }
          return providers
        }),
      )
      .handle(
        "provider",
        Effect.fn(function* (ctx) {
          const catalog = yield* Catalog.Service
          const pluginBoot = yield* PluginBoot.Service
          yield* pluginBoot.wait().pipe(Effect.catchDefect(() => Effect.fail(catalogUnavailable)))
          return yield* catalog.provider.get(ctx.params.providerID).pipe(
            Effect.catchTag("CatalogV2.ProviderNotFound", (error) =>
              Effect.fail(
                new ProviderNotFoundError({
                  providerID: error.providerID,
                  message: `Provider not found: ${error.providerID}`,
                }),
              ),
            ),
          )
        }),
      )
  }),
)
