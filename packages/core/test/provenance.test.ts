import { describe, expect, it } from "bun:test"
import path from "node:path"
import { Global } from "@opencode/util/global"
import { Provenance } from "@opencode/core/provenance"
import { Document, Info } from "@opencode/schema/config"
import { Config } from "@opencode/core/config"
import { Plugin } from "@opencode/core/plugin"
import { PluginHost } from "@opencode/core/plugin/host"
import { PluginHooks } from "@opencode/core/plugin/hooks"
import { Agent } from "@opencode/core/agent"
import { Session } from "@opencode/core/session"
import { SessionMessage } from "@opencode/core/session/message"
import { Tool } from "@opencode/schema/tool"
import { Effect, Schema } from "effect"
import { tmpdirScoped } from "./fixture/tmpdir"
import { testEffect } from "./lib/effect"
import { PluginTestLayer } from "./plugin/fixture"

describe("provenance helpers", () => {
  it("uses default state path when no custom file is given", () => {
    const file = Provenance.filePath()
    expect(file).toBe(path.join(Global.Path.state, "provenance", "events.jsonl"))
  })

  it("resolves relative paths under state path", () => {
    const file = Provenance.filePath("custom/events.jsonl")
    expect(file).toBe(path.join(Global.Path.state, "custom", "events.jsonl"))
  })

  it("keeps absolute custom paths unchanged", () => {
    const file = Provenance.filePath("/tmp/opencode/provenance/events.jsonl")
    expect(file).toBe("/tmp/opencode/provenance/events.jsonl")
  })

  it("truncates preview text when over max length", () => {
    expect(Provenance.preview("abcdef", 4)).toBe("abcd…")
  })

  it("produces stable hashes", () => {
    const first = Provenance.hash("hello")
    const second = Provenance.hash("hello")
    expect(first).toBe(second)
    expect(first).toHaveLength(64)
  })
})

testEffect(PluginTestLayer).live("writes bounded provenance and separates tool failures into incidents", () =>
  Effect.gen(function* () {
    const directory = yield* tmpdirScoped()
    const file = path.join(directory.path, "events.jsonl")
    const plugin = yield* Plugin.Service
    const host = yield* PluginHost.make(plugin)
    yield* Provenance.Plugin.effect(host).pipe(
      Effect.provide(
        Config.testLayer([
          new Document({
            type: "document",
            info: Schema.decodeUnknownSync(Info)({ provenance: { enabled: true, path: file, preview_chars: 4 } }),
          }),
        ]),
      ),
    )
    const hooks = yield* PluginHooks.Service
    const event = {
      tool: "write",
      sessionID: Session.ID.make("ses_provenance"),
      agent: Agent.ID.make("build"),
      messageID: SessionMessage.ID.create(),
      id: Tool.CallID.make("call_provenance"),
      input: { label: "abcde" },
    }
    yield* hooks.trigger("tool", "execute.after", { ...event, status: "completed", result: { content: "abcdef" } })
    const decode = Schema.decodeUnknownSync(Schema.fromJsonString(Schema.Json))
    expect(decode(yield* Effect.promise(() => Bun.file(file).text()))).toMatchObject({
      kind: "tool.completed",
      data: { input_preview: "{ la…", output_preview: "abcd…", input_hash: expect.stringMatching(/^[a-f0-9]{64}$/) },
    })
    yield* hooks.trigger("tool", "execute.after", {
      ...event,
      status: "error",
      error: new Tool.Error({ message: "denied" }),
    })
    expect(decode(yield* Effect.promise(() => Bun.file(Provenance.incidentFilePath(file)).text()))).toMatchObject({
      kind: "tool.error",
      level: "error",
      data: { error: "denied" },
    })
  }),
)
