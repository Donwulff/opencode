import { Bus } from "@/bus"
import { Command } from "@/command"
import { Config } from "@/config/config"
import { Global } from "@/global"
import { Installation } from "@/installation"
import { Instance } from "@/project/instance"
import { Session } from "@/session"
import { SessionCompaction } from "@/session/compaction"
import { MessageV2 } from "@/session/message-v2"
import { SessionStatus } from "@/session/status"
import { Lock } from "@/util/lock"
import { Log } from "@/util/log"
import { createHash, randomUUID } from "node:crypto"
import { appendFile, mkdir } from "node:fs/promises"
import path from "node:path"
import z from "zod"

export namespace Provenance {
  const log = Log.create({ service: "provenance" })

  const DEFAULT_FILE = path.join("provenance", "events.jsonl")
  const DEFAULT_PREVIEW = 240

  export const Entry = z.object({
    id: z.string(),
    ts: z.string(),
    source: z.literal("opencode"),
    version: z.string(),
    level: z.enum(["info", "warn", "error"]),
    kind: z.string(),
    project_id: z.string(),
    session_id: z.string().optional(),
    data: z.record(z.string(), z.any()).optional(),
  })

  type Entry = z.infer<typeof Entry>

  type State = {
    enabled: boolean
    file: string
    incidents: string
    preview: number
    seen: Set<string>
    unsub: (() => void)[]
  }

  function maxPreview(value?: number) {
    if (!value || value <= 0) return DEFAULT_PREVIEW
    return value
  }

  export function filePath(value?: string) {
    if (!value) return path.join(Global.Path.state, DEFAULT_FILE)
    if (path.isAbsolute(value)) return value
    return path.join(Global.Path.state, value)
  }

  export function incidentFilePath(value?: string) {
    return incidentPath(filePath(value))
  }

  function incidentPath(value: string) {
    return path.join(path.dirname(value), "incidents.jsonl")
  }

  export function preview(value: string, max: number) {
    if (value.length <= max) return value
    return value.slice(0, max) + "…"
  }

  export function stringify(value: unknown) {
    if (typeof value === "string") return value
    return Bun.inspect(value, {
      depth: 4,
      sorted: true,
      compact: true,
    })
  }

  export function hash(value: string) {
    return createHash("sha256").update(value).digest("hex")
  }

  function base(input: {
    kind: string
    level: Entry["level"]
    sessionID?: string
    data?: Record<string, unknown>
  }): Entry {
    return {
      id: randomUUID(),
      ts: new Date().toISOString(),
      source: "opencode",
      version: Installation.VERSION,
      level: input.level,
      kind: input.kind,
      project_id: Instance.project.id,
      session_id: input.sessionID,
      data: input.data,
    }
  }

  async function append(file: string, entry: Entry) {
    const line = JSON.stringify(entry) + "\n"
    using _ = await Lock.write(file)
    await appendFile(file, line)
  }

  async function write(input: {
    kind: string
    level: Entry["level"]
    sessionID?: string
    data?: Record<string, unknown>
  }) {
    const current = await state()
    if (!current.enabled) return
    const entry = base(input)
    await append(current.file, entry)
    if (entry.level !== "info") {
      await append(current.incidents, entry)
    }
  }

  async function tool(part: MessageV2.ToolPart) {
    const current = await state()
    if (!current.enabled) return
    if (part.state.status !== "completed" && part.state.status !== "error") return

    const end = part.state.time.end
    const seen = `${part.id}:${part.state.status}:${end}`
    if (current.seen.has(seen)) return
    current.seen.add(seen)

    const inputText = stringify(part.state.input)
    const toolData = {
      tool: part.tool,
      call_id: part.callID,
      duration_ms: end - part.state.time.start,
      input_hash: hash(inputText),
      input_preview: preview(inputText, current.preview),
    }

    if (part.state.status === "completed") {
      const outputText = stringify(part.state.output)
      await write({
        kind: "tool.completed",
        level: "info",
        sessionID: part.sessionID,
        data: {
          ...toolData,
          output_hash: hash(outputText),
          output_preview: preview(outputText, current.preview),
        },
      })
      return
    }

    const errorText = stringify(part.state.error)
    await write({
      kind: "tool.error",
      level: "error",
      sessionID: part.sessionID,
      data: {
        ...toolData,
        error_hash: hash(errorText),
        error_preview: preview(errorText, current.preview),
      },
    })
  }

  async function assistant(msg: MessageV2.Assistant) {
    const current = await state()
    if (!current.enabled) return
    if (!msg.time.completed) return

    const seen = `${msg.id}:${msg.time.completed}`
    if (current.seen.has(seen)) return
    current.seen.add(seen)

    await write({
      kind: "assistant.completed",
      level: msg.error ? "error" : "info",
      sessionID: msg.sessionID,
      data: {
        provider_id: msg.providerID,
        model_id: msg.modelID,
        agent: msg.agent,
        variant: msg.variant,
        cost: msg.cost,
        tokens: msg.tokens,
        finish: msg.finish,
        error: msg.error?.name,
      },
    })
  }

  async function mark(input: {
    kind: string
    level: Entry["level"]
    sessionID?: string
    data?: Record<string, unknown>
  }) {
    await write(input).catch((error) => {
      log.error("failed to write provenance event", {
        kind: input.kind,
        error,
      })
    })
  }

  const state = Instance.state(
    async (): Promise<State> => {
      const config = await Config.get()
      const enabled = config.provenance?.enabled ?? false
      const file = filePath(config.provenance?.path)
      const incidents = incidentPath(file)
      const previewChars = maxPreview(config.provenance?.preview_chars)

      const next: State = {
        enabled,
        file,
        incidents,
        preview: previewChars,
        seen: new Set<string>(),
        unsub: [],
      }

      if (!enabled) return next

      await mkdir(path.dirname(file), { recursive: true })
      await mkdir(path.dirname(incidents), { recursive: true })

      next.unsub.push(
        Bus.subscribe(Session.Event.Created, (event) => {
          void mark({
            kind: "session.created",
            level: "info",
            sessionID: event.properties.info.id,
            data: {
              title: event.properties.info.title,
              directory: event.properties.info.directory,
              parent_id: event.properties.info.parentID,
            },
          })
        }),
      )

      next.unsub.push(
        Bus.subscribe(Session.Event.Deleted, (event) => {
          void mark({
            kind: "session.deleted",
            level: "warn",
            sessionID: event.properties.info.id,
          })
        }),
      )

      next.unsub.push(
        Bus.subscribe(Session.Event.Error, (event) => {
          const error = event.properties.error
          if (!error) return
          const detail = error.data as Record<string, unknown>
          void mark({
            kind: "session.error",
            level: "error",
            sessionID: event.properties.sessionID,
            data: {
              error: error.name,
              message: typeof detail.message === "string" ? detail.message : undefined,
            },
          })
        }),
      )

      next.unsub.push(
        Bus.subscribe(Command.Event.Executed, (event) => {
          const argumentsText = event.properties.arguments.trim()
          void mark({
            kind: "command.executed",
            level: "info",
            sessionID: event.properties.sessionID,
            data: {
              name: event.properties.name,
              message_id: event.properties.messageID,
              arguments_hash: hash(argumentsText),
              arguments_preview: preview(argumentsText, previewChars),
            },
          })
        }),
      )

      next.unsub.push(
        Bus.subscribe(MessageV2.Event.Updated, (event) => {
          if (event.properties.info.role !== "assistant") return
          void assistant(event.properties.info).catch((error) => {
            log.error("failed to record assistant completion", {
              messageID: event.properties.info.id,
              error,
            })
          })
        }),
      )

      next.unsub.push(
        Bus.subscribe(MessageV2.Event.PartUpdated, (event) => {
          const part = event.properties.part
          if (part.type !== "tool") return
          void tool(part).catch((error) => {
            log.error("failed to record tool outcome", {
              partID: part.id,
              tool: part.tool,
              error,
            })
          })
        }),
      )

      next.unsub.push(
        Bus.subscribe(SessionStatus.Event.Status, (event) => {
          if (event.properties.status.type !== "retry") return
          void mark({
            kind: "session.retry",
            level: "warn",
            sessionID: event.properties.sessionID,
            data: {
              attempt: event.properties.status.attempt,
              message: event.properties.status.message,
              next: event.properties.status.next,
            },
          })
        }),
      )

      next.unsub.push(
        Bus.subscribe(SessionCompaction.Event.Compacted, (event) => {
          void mark({
            kind: "session.compacted",
            level: "info",
            sessionID: event.properties.sessionID,
          })
        }),
      )

      log.info("provenance enabled", {
        file,
        incidents,
        preview: previewChars,
      })

      return next
    },
    async (current) => {
      for (const unsub of current.unsub) {
        unsub()
      }
    },
  )

  export function init() {
    void state()
  }

  export async function paths() {
    const current = await state()
    return {
      enabled: current.enabled,
      file: current.file,
      incidents: current.incidents,
    }
  }
}
