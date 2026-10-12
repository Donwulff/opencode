export * as ConfigInstructionPlugin from "./instruction.js"

import { define } from "@opencode/plugin/effect/plugin"
import { FSUtil } from "@opencode/util/fs-util"
import { Global } from "@opencode/util/global"
import { sameDirectory } from "@opencode/util/path"
import { dirname, join, resolve } from "path"
import { Effect, Option, PubSub, Semaphore, Stream } from "effect"
import { HttpClient, HttpClientRequest } from "effect/http"
import { Config } from "../../config.js"
import { Security } from "../../util/security.js"
import { Watcher } from "../../filesystem/watcher.js"
import { InstructionDiscovery } from "../../instruction-discovery.js"
import { Instructions } from "../../instructions/index.js"
import { Location } from "../../location.js"
import { AbsolutePath } from "../../schema.js"

type Loaded =
  | { readonly type: "available"; readonly files: InstructionDiscovery.File[] }
  | { readonly type: "unavailable" }

export const Plugin = define({
  id: "opencode.config.instruction",
  effect: Effect.fn(function* (ctx) {
    const config = Option.getOrUndefined(yield* Effect.serviceOption(Config.Service))
    const http = Option.getOrUndefined(yield* Effect.serviceOption(HttpClient.HttpClient))
    const discovery = yield* InstructionDiscovery.Service
    // Nothing this plugin watches or loads can contribute when both scopes
    // are disabled; skip the resolves, the watcher fiber, and the transform.
    if (!discovery.project && !discovery.global) return
    yield* Effect.gen(function* () {
      const fs = yield* FSUtil.Service
      const global = yield* Global.Service
      const location = yield* Location.Service
      const watcher = yield* Watcher.Service
      const changes = yield* PubSub.sliding<string>(1)
      const lock = Semaphore.makeUnsafe(1)
      const start = yield* fs.resolve(location.directory)
      const root = yield* fs.resolve(location.project.directory)
      const home = yield* fs.resolve(global.home)
      const project = discovery.project && FSUtil.contains(root, start)
      const stop = FSUtil.contains(home, start) ? home : root
      const globalFile = yield* fs.resolve(join(global.config, "AGENTS.md"))
      const loaded: { current: Loaded } = { current: { type: "available", files: [] } }

      const publish = (update: Watcher.Update) => PubSub.publish(changes, update.path).pipe(Effect.asVoid)
      // The ancestor walk can reach the global file when the location sits
      // beneath the global config dir; global: false excludes it there too.
      const candidates = [
        ...(discovery.global ? [globalFile] : []),
        ...(project
          ? ancestorDirectories(start, stop)
              .map((directory) => join(directory, "AGENTS.md"))
              .filter((file) => discovery.global || file !== globalFile)
          : []),
      ]
      for (const path of new Set(candidates)) {
        const updates = yield* watcher.subscribe({ path, type: "file" })
        yield* updates.pipe(Stream.runForEach(publish), Effect.forkScoped({ startImmediately: true }))
      }

      const read = Effect.fn("ConfigInstructionPlugin.read")(function* (path: string) {
        const content = yield* fs.readFileStringSafe(path)
        if (content !== undefined) return new InstructionDiscovery.File({ path: AbsolutePath.make(path), content })
        yield* Effect.logDebug("instruction file skipped", { path, reason: "unavailable" })
      })

      const globalSource = Effect.fn("ConfigInstructionPlugin.globalSource")(function* () {
        if (!discovery.global || !(yield* fs.isFile(globalFile))) return []
        const file = yield* read(globalFile)
        return file ? [file] : []
      })

      const projectSource = Effect.fn("ConfigInstructionPlugin.projectSource")(function* () {
        if (!project) return []
        const walked = yield* Effect.forEach(
          yield* fs.up({ targets: ["AGENTS.md"], start, stop, type: "file" }),
          fs.resolve,
        )
        const discovered = new Set(walked.filter((file) => discovery.global || file !== globalFile))
        const files = yield* Effect.forEach(discovered, read, { concurrency: "unbounded" })
        if (files.some((file) => file === undefined)) return Instructions.unavailable
        return files.filter((file): file is InstructionDiscovery.File => file !== undefined)
      })

      const configuredSource = Effect.fn("ConfigInstructionPlugin.configuredSource")(function* () {
        if (!config) return []
        const entries = (yield* config.entries()).flatMap((entry) =>
          entry.type === "document"
            ? (entry.info.instructions ?? []).map((source) => ({
                source,
                directory: entry.path ? dirname(entry.path) : location.directory,
              }))
            : [],
        )
        return yield* Effect.forEach(entries, ({ source, directory }) =>
          Effect.gen(function* () {
            if (!/^https?:\/\//.test(source)) {
              const file = yield* read(resolve(directory, source))
              return file ? [file] : []
            }
            if (!http) return []
            yield* Security.assertUrl(config, source, "instruction")
            const content = yield* HttpClientRequest.get(source).pipe(
              http.execute,
              Effect.flatMap((response) => response.text),
              Effect.timeout("10 seconds"),
            )
            return [new InstructionDiscovery.File({ path: source, content: Security.sanitizeForLLM(content) })]
          }),
        ).pipe(Effect.map((files) => files.flat()))
      })

      const isolate = <A, E, R>(source: string, effect: Effect.Effect<A, E, R>) =>
        effect.pipe(
          Effect.catchCause((cause) =>
            Effect.logWarning("failed to load instruction source", { source, cause }).pipe(
              Effect.as(Instructions.unavailable),
            ),
          ),
        )

      const refresh = Effect.fn("ConfigInstructionPlugin.refresh")(
        function* (file?: string) {
          const sources = yield* Effect.all({
            global: isolate("global", globalSource()),
            project: isolate("project", projectSource()),
            configured: isolate("configured", configuredSource()),
          })
          loaded.current =
            Array.isArray(sources.global) && Array.isArray(sources.project) && Array.isArray(sources.configured)
              ? { type: "available", files: [...sources.global, ...sources.project, ...sources.configured] }
              : { type: "unavailable" }
          if (!file) return
          yield* Effect.logDebug("instructions rescanned", {
            file,
            instructions:
              loaded.current.type === "available" ? loaded.current.files.map((item) => item.path) : "unavailable",
          })
        },
        (effect, ..._args: [file?: string]) => lock.withPermit(effect),
      )

      // Editor saves arrive as bursts of watcher events; settle before rescanning once. Subscribe
      // before debouncing so no update slips through while the debounce starts its pull.
      const updates = yield* PubSub.subscribe(changes)
      yield* Stream.fromSubscription(updates).pipe(
        Stream.debounce("100 millis"),
        Stream.runForEach((file) => refresh(file).pipe(Effect.andThen(discovery.reload()))),
        Effect.forkScoped({ startImmediately: true }),
      )
      yield* ctx.event.subscribe().pipe(
        Stream.filter((event) => event.type === "config.updated"),
        Stream.runForEach(() => refresh().pipe(Effect.andThen(discovery.reload()))),
        Effect.forkScoped({ startImmediately: true }),
      )
      yield* refresh()
      yield* discovery.transform((editor) => {
        if (loaded.current.type === "unavailable") {
          editor.unavailable()
          return
        }
        for (const file of loaded.current.files) editor.add(file)
      })
    }).pipe(
      Effect.catchCause((cause) =>
        Effect.logWarning("failed to activate instruction source", { cause }).pipe(
          Effect.andThen(discovery.transform((editor) => editor.unavailable())),
          Effect.asVoid,
        ),
      ),
    )
  }),
})

// `start` keeps the client's spelling while `stop` may come from git, so a Windows drive
// letter can differ only in case (`c:\repo` vs `C:\repo`). Compare the way FSUtil.contains
// admitted `start` beneath `stop`, or the walk passes `stop` and recurses at the drive root.
function ancestorDirectories(start: string, stop: string): string[] {
  if (sameDirectory(start, stop)) return [start]
  return [start, ...ancestorDirectories(dirname(start), stop)]
}
