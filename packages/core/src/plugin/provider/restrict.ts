import { define } from "../internal"
import { Effect } from "effect"
import { Flag } from "../../flag/flag"
import { Config } from "../../config"
import { validateUrlFromConfig, type SecurityConfigType } from "../../util/network"

// Fork feature: hide providers that are not explicitly configured, and (defense-in-depth)
// hide providers whose endpoint URL is blocked by the SecurityConfig. Driven by the
// OPENCODE_RESTRICT_PROVIDERS env flag, the `restrict_to_configured_providers` config field,
// or a `security` config block. No-op unless one of those is set, so this is inert in stock
// builds.
//
// Registered last in PluginInternal so it runs after the provider/config plugins that enable
// providers — disabling here has the final say. Works on the v2 catalog path the TUI model
// selector uses: `catalog.provider.available()` filters out providers whose `disabled` is
// truthy, so setting `disabled = true` removes them from the selector.
export const RestrictProvidersPlugin = define({
  id: "restrict-providers",
  effect: Effect.fn(function* (ctx) {
    const config = yield* Config.Service
    const documents = (yield* config.entries()).filter(
      (entry): entry is Config.Document => entry.type === "document",
    )
    // A config value (true or false) from the highest-priority document overrides the env
    // flag; entries() is ordered lowest -> highest priority, so take the last defined value.
    const override = documents
      .map((doc) => doc.info.restrict_to_configured_providers)
      .findLast((value) => value !== undefined)
    const restrict = override ?? Flag.OPENCODE_RESTRICT_PROVIDERS
    // Highest-priority `security` block wins, mirroring config merge precedence.
    const security = documents.map((doc) => doc.info.security).findLast((value) => value !== undefined) as
      | SecurityConfigType
      | undefined
    if (!restrict && !security) return

    const configured = new Set(documents.flatMap((doc) => Object.keys(doc.info.providers ?? {})))

    yield* ctx.catalog.transform(
      Effect.fn(function* (catalog) {
        for (const item of catalog.provider.list()) {
          const blockedByRestrict = restrict && !configured.has(item.provider.id)
          const url = item.provider.api.url
          const blockedByUrl = !!security && !!url && !validateUrlFromConfig(url, security).allowed
          if (!blockedByRestrict && !blockedByUrl) continue
          catalog.provider.update(item.provider.id, (provider) => {
            provider.disabled = true
          })
        }
      }),
    )
  }),
})
