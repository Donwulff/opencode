import { Effect } from "effect"
import { Flag } from "../../flag/flag"
import { Config } from "../../config"
import { PluginV2 } from "../../plugin"

// Fork feature: hide providers that are not explicitly configured. Driven by the
// OPENCODE_RESTRICT_PROVIDERS env flag or the `restrict_to_configured_providers` config
// field. No-op unless one of those is set, so this is inert in stock builds.
//
// Registered last in PluginBoot so it runs after the provider/config plugins that enable
// providers — disabling here has the final say. Works on the v2 catalog path the TUI model
// selector uses: `catalog.provider.available()` returns only providers whose `enabled` is
// truthy, so setting `enabled = false` removes them from the selector.
//
// Endpoint-URL validation against the fork's SecurityConfig is intentionally NOT done here:
// that config lives in the opencode package, which core cannot import. It remains enforced
// in the v1 provider + config.providers HTTP handlers.
export const RestrictProvidersPlugin = PluginV2.define({
  id: PluginV2.ID.make("restrict-providers"),
  effect: Effect.gen(function* () {
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
    if (!restrict) return

    const configured = new Set(documents.flatMap((doc) => Object.keys(doc.info.providers ?? {})))

    return {
      "catalog.transform": Effect.fn(function* (evt) {
        for (const item of evt.provider.list()) {
          if (configured.has(item.provider.id)) continue
          evt.provider.update(item.provider.id, (provider) => {
            provider.enabled = false
          })
        }
      }),
    }
  }),
})
