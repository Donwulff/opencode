import path from "node:path"
import { describe, expect } from "bun:test"
import { Effect, Layer, Stream } from "effect"
import { AppNodeBuilder } from "@opencode/core/effect/app-node-builder"
import { Artifact } from "@opencode/core/artifact"
import { Bus } from "@opencode/core/bus"
import { Config } from "@opencode/core/config"
import { Database } from "@opencode/core/database/database"
import { Flag } from "@opencode/core/flag"
import { Image } from "@opencode/core/image"
import { Location } from "@opencode/core/location"
import { Permission } from "@opencode/core/permission"
import { Session } from "@opencode/core/session"
import { SessionInbox } from "@opencode/core/session/inbox"
import { SessionStore } from "@opencode/core/session/store"
import { Tool } from "@opencode/core/tool"
import { ArtifactTools } from "@opencode/core/tool/plugin/artifact"
import { LayerNode } from "@opencode/util/effect/layer-node"
import { Global } from "@opencode/util/global"
import { Document, Info } from "@opencode/schema/config"
import { ConfigExperimental } from "@opencode/schema/config/experimental"
import { AbsolutePath } from "@opencode/schema/schema"
import { tmpdirScoped } from "./fixture/tmpdir"
import { offlineModels } from "./fixture/models"
import { testEffect } from "./lib/effect"
import { imagePassthrough } from "./lib/image"
import { permissionLayer } from "./lib/permission"
import { codeModeListings, executeTool, registerToolPlugin, toolIdentity } from "./lib/tool"

const toolPaths = [
  "opencode.artifact_create",
  "opencode.artifact_read",
  "opencode.artifact_update",
  "opencode.artifact_delete",
]

const buildLayer = (artifactsEnabled: boolean, dir: string) => {
  const entries = artifactsEnabled
    ? [
        new Document({
          type: "document",
          info: new Info({
            experimental: new ConfigExperimental.Info({ artifacts: true }),
          }),
        }),
      ]
    : []
  const ref = Location.Ref.make({ directory: AbsolutePath.make(dir) })
  return AppNodeBuilder.build(
    LayerNode.group([
      Artifact.node,
      Bus.node,
      Config.node,
      Database.node,
      Flag.node,
      Global.node,
      Location.node,
      Permission.node,
      Session.node,
      SessionStore.node,
      Tool.node,
    ]),
    [
      Config.node.replace(Config.testLayer(entries)),
      Global.node.replace(Global.layerWith({ data: path.join(dir, ".data") })),
      Image.node.replace(imagePassthrough),
      Location.node.replace(Location.boundNode(ref)),
      Permission.node.replace(permissionLayer({ assert: () => Effect.void })),
      offlineModels,
    ],
  )
}

const callCodeMode = (registry: Tool.Interface, sessionID: Session.ID, name: string, input: unknown) =>
  executeTool(registry, {
    sessionID,
    ...toolIdentity,
    call: {
      type: "tool-call",
      id: `call-${name}`,
      name: "execute",
      input: { code: `return await tools.opencode.${name}(${JSON.stringify(input)})` },
    },
  }).pipe(
    Effect.map((result) => {
      if (result.status !== "completed") {
        throw new Error(`Tool ${name} failed: ${JSON.stringify(result.error)}`)
      }
      return JSON.parse(result.content?.[0]?.type === "text" ? result.content[0].text : "")
    }),
  )

const it = testEffect(Layer.empty)

describe("experimental.artifacts service and tools", () => {
  for (const command of ["review", "learn"]) {
    it.live(`blocks artifact mutations during /${command} until ordinary input arrives`, () =>
      Effect.gen(function* () {
        const dir = (yield* tmpdirScoped("opencode-artifact-command-")).path
        yield* Effect.gen(function* () {
          const artifacts = yield* Artifact.Service
          const registry = yield* Tool.Service
          const sessions = yield* Session.Service
          const database = yield* Database.Service
          const bus = yield* Bus.Service
          const session = yield* sessions.create({ location: Location.Ref.make({ directory: AbsolutePath.make(dir) }) })
          const info = yield* artifacts.create({ title: "Original", files: { "index.html": "original" } })
          yield* registerToolPlugin(ArtifactTools.Plugin, { event: { subscribe: () => Stream.never } })
          yield* sessions.prompt({ sessionID: session.id, text: "inspect", command, resume: false })
          yield* SessionInbox.promote(database.db, bus, session.id, "steer")

          const mutations = [
            { name: "artifact_create", input: { title: "Blocked", files: { "index.html": "blocked" } } },
            { name: "artifact_update", input: { id: info.id, files: { "index.html": "blocked" } } },
            { name: "artifact_delete", input: { id: info.id } },
          ]
          for (const mutation of mutations) {
            const result = yield* executeTool(registry, {
              sessionID: session.id,
              ...toolIdentity,
              call: {
                type: "tool-call",
                id: `call-${mutation.name}`,
                name: "execute",
                input: { code: `return await tools.opencode.${mutation.name}(${JSON.stringify(mutation.input)})` },
              },
            })
            expect(result.status).toBe("completed")
            expect(result.output?.error).toBe(true)
            expect(result.output?.output).toContain(`/${command}`)
          }
          expect((yield* artifacts.list()).map((item) => item.id)).toEqual([info.id])
          expect((yield* callCodeMode(registry, session.id, "artifact_read", { id: info.id })).files).toEqual({
            "index.html": "original",
          })

          yield* sessions.prompt({ sessionID: session.id, text: "publish", resume: false })
          yield* SessionInbox.promote(database.db, bus, session.id, "steer")
          yield* callCodeMode(registry, session.id, "artifact_update", {
            id: info.id,
            files: { "index.html": "updated" },
          })
          expect((yield* artifacts.get(info.id)).files).toEqual({ "index.html": "updated" })
          yield* callCodeMode(registry, session.id, "artifact_create", {
            title: "Allowed",
            files: { "index.html": "allowed" },
          })
          yield* callCodeMode(registry, session.id, "artifact_delete", { id: info.id })
          expect((yield* artifacts.list()).map((item) => item.title)).toEqual(["Allowed"])
        }).pipe(Effect.provide(buildLayer(true, dir)))
      }),
    )
  }

  it.live("stays disabled and hides artifact tools when experimental.artifacts is unset", () =>
    Effect.gen(function* () {
      const dir = (yield* tmpdirScoped("opencode-artifact-off-")).path
      yield* Effect.gen(function* () {
        const artifacts = yield* Artifact.Service
        const registry = yield* Tool.Service
        expect(yield* artifacts.enabled()).toBe(false)
        expect(
          yield* artifacts.create({ title: "Draft", files: { "index.html": "<h1>hi</h1>" } }).pipe(Effect.flip),
        ).toBeInstanceOf(Artifact.DisabledError)

        yield* registerToolPlugin(ArtifactTools.Plugin, {
          event: { subscribe: () => Stream.never },
        })
        const snap = yield* registry.snapshot()
        const listed = snap.codeModeCatalog ? codeModeListings(snap.codeModeCatalog).map((item) => item.path) : []
        for (const name of toolPaths) {
          expect(listed).not.toContain(name)
        }
      }).pipe(Effect.provide(buildLayer(false, dir)))
    }),
  )

  it.live("registers custom providers, routes overrides, and runs the 4 CRUD tools", () =>
    Effect.gen(function* () {
      const dir = (yield* tmpdirScoped("opencode-artifact-on-")).path

      yield* Effect.gen(function* () {
        const artifacts = yield* Artifact.Service
        const registry = yield* Tool.Service
        const sessions = yield* Session.Service
        const session = yield* sessions.create({
          location: Location.Ref.make({ directory: AbsolutePath.make(dir) }),
        })

        const remoteStore = new Map<Artifact.ID, Record<string, Uint8Array>>()
        yield* artifacts.transform((editor) => {
          editor.add({
            id: Artifact.ProviderID.make("cloudflare"),
            name: "Cloudflare",
            capabilities: ["static", "server-code", "sqlite", "durable-objects"],
            prompt: "Artifacts deploy to Cloudflare with SQLite and Durable Objects.",
            create: (input) =>
              Effect.sync(() => {
                remoteStore.set(input.id, { ...input.files })
                return { url: `https://cf.example.test/${input.id}/` }
              }),
            read: (id) => Effect.sync(() => remoteStore.get(id) ?? {}),
            update: (id, input) =>
              Effect.sync(() => {
                remoteStore.set(id, { ...input.files })
                return { url: `https://cf.example.test/${id}/` }
              }),
            delete: (id) =>
              Effect.sync(() => {
                remoteStore.delete(id)
              }),
          })
          editor.default.set(Artifact.ProviderID.make("cloudflare"))
        })

        yield* registerToolPlugin(ArtifactTools.Plugin, {
          event: { subscribe: () => Stream.never },
        })
        const snap = yield* registry.snapshot()
        const listings = snap.codeModeCatalog ? codeModeListings(snap.codeModeCatalog) : []
        expect(listings.map((item) => item.path)).toEqual(expect.arrayContaining(toolPaths))
        const rawTools = yield* registry.list()
        const createTool = rawTools.find((item) => item.id === "opencode_artifact_create")
        expect(createTool?.description).toContain(
          "Default provider (cloudflare [static, server-code, sqlite, durable-objects]): Artifacts deploy to Cloudflare with SQLite and Durable Objects.",
        )
        expect(createTool?.description).toContain("Other active providers (pass provider to override): local [static].")

        const createdInfo = (yield* callCodeMode(registry, session.id, "artifact_create", {
          title: "Flamegraph report",
          files: {
            "index.html": "<!doctype html><script src='./assets/app.js'></script>",
            "assets/app.js": "console.log('v1')",
          },
        })) as Artifact.Info

        expect(createdInfo).toMatchObject({
          provider: "cloudflare",
          sessionID: session.id,
          title: "Flamegraph report",
          entry: "index.html",
          url: `https://cf.example.test/${createdInfo.id}/`,
        })

        const content = (yield* callCodeMode(registry, session.id, "artifact_read", {
          id: createdInfo.id,
        })) as Artifact.Content
        expect(Object.keys(content.files).toSorted()).toEqual(["assets/app.js", "index.html"])
        expect(content.files["assets/app.js"]).toBe("console.log('v1')")

        // Per-request provider override ("local") stores independently of default ("cloudflare")
        const localInfo = (yield* callCodeMode(registry, session.id, "artifact_create", {
          title: "Local scratch",
          files: { "index.html": "<h1>Notes</h1>" },
          provider: "local",
        })) as Artifact.Info
        expect(localInfo.provider).toBe(Artifact.LocalProviderID)

        // Update replaces files at the same ID and URL
        const updatedInfo = (yield* callCodeMode(registry, session.id, "artifact_update", {
          id: createdInfo.id,
          files: { "index.html": "<h1>v2</h1>" },
        })) as Artifact.Info
        expect(updatedInfo.url).toBe(createdInfo.url)

        yield* callCodeMode(registry, session.id, "artifact_delete", { id: createdInfo.id })
        expect(yield* artifacts.get(createdInfo.id).pipe(Effect.flip)).toBeInstanceOf(Artifact.NotFoundError)

        // Reject path traversal
        const traversal = yield* artifacts
          .create({
            title: "Bad path",
            files: { "../escape.html": "nope" },
          })
          .pipe(Effect.flip)
        expect(traversal).toBeInstanceOf(Artifact.InvalidError)
      }).pipe(Effect.provide(buildLayer(true, dir)))
    }),
  )
})
