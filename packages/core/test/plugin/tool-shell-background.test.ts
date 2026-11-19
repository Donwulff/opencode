import { expect } from "bun:test"
import { Effect, Layer } from "effect"
import { Agent } from "@opencode/core/agent"
import { AppNodeBuilder } from "@opencode/core/effect/app-node-builder"
import { Model } from "@opencode/core/model"
import { Plugin } from "@opencode/core/plugin"
import { PluginHooks } from "@opencode/core/plugin/hooks"
import { PluginHost } from "@opencode/core/plugin/host"
import { Location } from "@opencode/core/location"
import { Provider } from "@opencode/core/provider"
import { Session } from "@opencode/core/session"
import { Shell } from "@opencode/core/shell"
import { ShellSelect } from "@opencode/core/shell/select"
import { ShellTool } from "@opencode/core/tool/plugin/shell"
import { Tool } from "@opencode/core/tool"
import { Global } from "@opencode/util/global"
import { LayerNode } from "@opencode/util/effect/layer-node"
import type { SessionHooks } from "@opencode/plugin/effect/session"
import { tempGlobalLayer } from "../fixture/global"
import { tempLocationLayer } from "../fixture/location"
import { testEffect } from "../lib/effect"
import { toolDefinitions } from "../lib/tool"
import { PluginTestLayer } from "./fixture"

const it = testEffect(
  Layer.merge(
    PluginTestLayer,
    AppNodeBuilder.build(LayerNode.group([Shell.node, Shell.daemonNode, ShellSelect.node]), [
      Global.node.replace(tempGlobalLayer),
      Location.node.replace(tempLocationLayer),
    ]),
  ),
)

const ref = (providerID: string, id: string, variant?: string) =>
  Model.Ref.make({
    providerID: Provider.ID.make(providerID),
    id: Model.ID.make(id),
    ...(variant ? { variant: Model.VariantID.make(variant) } : {}),
  })

const providerID = Provider.ID.make("test")
const model = (id: string, overlays: Partial<Model.Info> = {}) => ({ ...Model.Info.default(providerID, Model.ID.make(id)), ...overlays })

it.effect("hides background from a model that forbids it and leaves other models unchanged", () =>
  Effect.gen(function* () {
    const providers = yield* Provider.Service
    const plugins = yield* Plugin.Service
    const hooks = yield* PluginHooks.Service
    const registry = yield* Tool.Service
    yield* providers.transform((editor) =>
      editor.add({
        info: { ...Provider.Info.empty(providerID), package: "@opencode/ai/providers/openai" },
        models: [
          model("plain"),
          model("disabled", { settings: { shell: { background: false } } }),
          model("enabled", { settings: { shell: { background: true } } }),
          model("variant", {
            variants: [{ id: Model.VariantID.make("quiet"), settings: { shell: { background: false } } }],
          }),
          model("variant-override", {
            settings: { shell: { background: false } },
            variants: [{ id: Model.VariantID.make("normal"), settings: { shell: { background: true } } }],
          }),
          // Malformed settings are ignored.
          model("shell-string", { settings: { shell: "no" } }),
          model("background-string", { settings: { shell: { background: "no" } } }),
          model("shell-null", { settings: { shell: null } }),
        ],
      }),
    )
    yield* ShellTool.Plugin.effect(yield* PluginHost.make(plugins, "test"))
    const definition = (yield* toolDefinitions(registry)).find((tool) => tool.name === "shell")
    if (!definition) throw new Error("shell tool not registered")
    const advertised = definition.inputSchema
    const event = (modelRef: Model.Ref): SessionHooks["context"] => ({
      sessionID: Session.ID.make("ses_shell_background"),
      agent: Agent.ID.make("build"),
      model: modelRef,
      system: [],
      messages: [],
      tools: { shell: { description: definition.description, input: JSON.parse(JSON.stringify(advertised)) } },
      options: {},
    })

    // The registered schema advertises background; the hook hides it per model.
    expect(advertised).toMatchObject({ properties: { background: { type: "boolean" } } })
    expect(advertised).toMatchObject({
      properties: {
        timeout: {
          type: "integer",
          minimum: 0,
          description:
            "Optional timeout in milliseconds (default 120000). When it expires, a foreground command moves to the background and a background command is stopped.",
        },
      },
    })

    for (const kind of ["context", "compaction", "generate"] as const) {
      const disabled = event(ref("test", "disabled"))
      yield* hooks.trigger("session", kind, disabled)
      const input = disabled.tools.shell.input
      expect(input).not.toHaveProperty("properties.background")
      expect(input).toMatchObject({
        properties: {
          timeout: {
            type: "integer",
            minimum: 0,
            description: "Optional timeout in milliseconds (default 120000). When it expires, the command is stopped.",
          },
        },
      })
      expect(disabled.tools.shell.description).toContain("Commands execute in the foreground.")
      expect(disabled.tools.shell.description).toContain("The default timeout is 120000 ms.")
      expect(disabled.tools.shell.description).toContain("Specify a longer timeout for tests or builds expected to exceed this duration.")
      expect(disabled.tools.shell.description).toContain("the command is stopped rather than backgrounded.")
      expect(disabled.tools.shell.description).not.toContain("move to the background")
      expect(disabled.tools.shell.description).not.toContain("Background commands return immediately")
    }

    const quiet = event(ref("test", "variant", "quiet"))
    yield* hooks.trigger("session", "context", quiet)
    expect(quiet.tools.shell.input).not.toHaveProperty("properties.background")

    for (const modelRef of [
      ref("test", "plain"),
      ref("test", "enabled"),
      ref("test", "variant"),
      ref("test", "variant-override", "normal"),
      ref("unknown", "model"),
    ]) {
      const kept = event(modelRef)
      yield* hooks.trigger("session", "context", kept)
      expect(kept.tools.shell.input).toHaveProperty("properties.background")
      expect(kept.tools.shell.input).toMatchObject({
        properties: {
          timeout: {
            description:
              "Optional timeout in milliseconds (default 120000). When it expires, a foreground command moves to the background and a background command is stopped.",
          },
        },
      })
      expect(kept.tools.shell.description).toContain("Commands still running after their timeout move to the background.")
      expect(kept.tools.shell.description).toContain("Background commands return immediately")
    }

    for (const id of ["shell-string", "background-string", "shell-null"]) {
      const malformed = event(ref("test", id))
      yield* hooks.trigger("session", "context", malformed)
      expect(malformed.tools.shell.input).toHaveProperty("properties.background")
    }
  }),
)
