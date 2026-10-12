export * as Provenance from "./provenance.js"

import { createHash, randomUUID } from "node:crypto"
import { appendFile, mkdir } from "node:fs/promises"
import { inspect } from "node:util"
import path from "path"
import { define } from "@opencode/plugin/effect/plugin"
import { Global } from "@opencode/util/global"
import { Effect, Stream } from "effect"
import { Config } from "./config.js"
import { Location } from "./location.js"

export const filePath = (value?: string) =>
  value && path.isAbsolute(value) ? value : path.join(Global.Path.state, value ?? "provenance/events.jsonl")
export const incidentFilePath = (value?: string) => path.join(path.dirname(filePath(value)), "incidents.jsonl")
export const preview = (value: string, max: number) => (value.length <= max ? value : `${value.slice(0, max)}…`)
export const stringify = (value: unknown) =>
  typeof value === "string" ? value : inspect(value, { depth: 4, sorted: true, compact: true })
export const hash = (value: string) => createHash("sha256").update(value).digest("hex")

export const Plugin = define({
  id: "opencode.provenance",
  effect: Effect.fn(function* (ctx) {
    const config = yield* Config.Service
    const location = yield* Location.Service
    const write = Effect.fn("Provenance.write")(function* (
      kind: string,
      sessionID: string | undefined,
      data: Record<string, unknown>,
      level = "info",
    ) {
      const settings = Config.latest(yield* config.entries(), "provenance")
      if (!settings?.enabled) return
      const file = filePath(settings.path)
      const line =
        JSON.stringify({
          id: randomUUID(),
          ts: new Date().toISOString(),
          source: "opencode",
          version: ctx.app.version,
          level,
          kind,
          project_id: location.project.id,
          session_id: sessionID,
          data,
        }) + "\n"
      yield* Effect.promise(async () => {
        await mkdir(path.dirname(file), { recursive: true })
        await appendFile(file, line)
        if (level !== "info") await appendFile(incidentFilePath(settings.path), line)
      })
    })
    yield* ctx.tool.hook("execute.after", (event) =>
      Effect.gen(function* () {
        const settings = Config.latest(yield* config.entries(), "provenance")
        if (!settings?.enabled) return
        const size = settings.preview_chars && settings.preview_chars > 0 ? settings.preview_chars : 240
        const input = stringify(event.input)
        yield* write(
          event.status === "completed" ? "tool.completed" : "tool.error",
          event.sessionID,
          {
            tool: event.tool,
            input_hash: hash(input),
            input_preview: preview(input, size),
            ...(event.status === "completed"
              ? {
                  output_hash: hash(stringify(event.result.content)),
                  output_preview: preview(stringify(event.result.content), size),
                }
              : { error: event.error.message }),
          },
          event.status === "completed" ? "info" : "error",
        )
      }).pipe(Effect.catchCause((cause) => Effect.logError("failed to write provenance event", { cause }))),
    )
    yield* ctx.event.subscribe().pipe(
      Stream.filter(
        (event) =>
          event.type === "session.created" ||
          event.type === "session.deleted" ||
          event.type === "session.execution.failed" ||
          event.type === "session.retry.scheduled" ||
          event.type === "session.step.ended",
      ),
      Stream.runForEach((event) =>
        write(
          event.type,
          event.data.sessionID,
          event.type === "session.step.ended"
            ? {
                assistant_message_id: event.data.assistantMessageID,
                finish: event.data.finish,
                cost: event.data.cost,
                tokens: event.data.tokens,
              }
            : { event: event.type },
          event.type === "session.execution.failed" ? "error" : "info",
        ).pipe(Effect.catchCause((cause) => Effect.logError("failed to write provenance event", { cause }))),
      ),
      Effect.forkScoped({ startImmediately: true }),
    )
  }),
})
