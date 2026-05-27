import { Config } from "@/config/config"
import { Flag } from "@opencode-ai/core/flag/flag"
import { Provider } from "@/provider/provider"
import { validateUrlFromConfig, type SecurityConfigType } from "@/util/network"
import * as InstanceState from "@/effect/instance-state"
import { Effect } from "effect"
import { HttpApiBuilder } from "effect/unstable/httpapi"
import { InstanceHttpApi } from "../api"
import { markInstanceForDisposal } from "../lifecycle"

export const configHandlers = HttpApiBuilder.group(InstanceHttpApi, "config", (handlers) =>
  Effect.gen(function* () {
    const providerSvc = yield* Provider.Service
    const configSvc = yield* Config.Service

    const get = Effect.fn("ConfigHttpApi.get")(function* () {
      return yield* configSvc.get()
    })

    const update = Effect.fn("ConfigHttpApi.update")(function* (ctx) {
      yield* configSvc.update(ctx.payload)
      yield* markInstanceForDisposal(yield* InstanceState.context)
      return ctx.payload
    })

    const providers = Effect.fn("ConfigHttpApi.providers")(function* () {
      const loaded = yield* providerSvc.list()
      let providerList = Object.values(loaded)

      const cfg = yield* configSvc.get()
      const restrictToConfigured = cfg.restrict_to_configured_providers ?? Flag.OPENCODE_RESTRICT_PROVIDERS
      if (restrictToConfigured) {
        const configuredSet = new Set(Object.keys(cfg.provider ?? {}))
        providerList = providerList.filter((p) => configuredSet.has(p.id))
      }

      const security = cfg.security as SecurityConfigType | undefined
      if (security) {
        providerList = providerList.filter((p) => {
          const urls = Object.values(p.models).map((m) => m.api?.url).filter(Boolean)
          if (urls.length === 0) return true
          return urls.some((url) => validateUrlFromConfig(url!, security).allowed)
        })
      }

      return {
        providers: providerList.map(Provider.toPublicInfo),
        default: Provider.defaultModelIDs(
          Object.fromEntries(providerList.map((p) => [p.id, p])),
        ),
      }
    })

    return handlers.handle("get", get).handle("update", update).handle("providers", providers)
  }),
)
