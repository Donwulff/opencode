import { Config } from "@/config/config"
import { Global } from "@opencode-ai/core/global"
import { Installation } from "@/installation"
import { InstallationVersion } from "@opencode-ai/core/installation/version"
import { Instance } from "@/project/instance"
import { SessionV1 } from "@opencode-ai/core/v1/session"
import { Lock } from "@/util/lock"
import * as Log from "@opencode-ai/core/util/log"
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
      version: InstallationVersion,
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

  async function tool(part: SessionV1.ToolPart) {
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

  async function assistant(msg: SessionV1.Assistant) {
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

  const stateCache = new Map<string, Promise<State>>()

  function state(): Promise<State> {
    const dir = Instance.directory
    const existing = stateCache.get(dir)
    if (existing) return existing
    const promise = initState()
    stateCache.set(dir, promise)
    return promise
  }

  async function initState(): Promise<State> {
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

      // Event subscriptions disabled pending port from Bus.subscribe to EventV2 streams.
      // Upstream removed Bus.subscribe in the database-schema-ownership refactor (#29068)
      // — provenance write/paths APIs still work, but live event capture is off until a
      // SessionLegacy.Event listener is wired through the new EventV2.Service. Tracked in
      // FORK-CHANGES.md §2.
      void tool
      void assistant
      void mark

      log.info("provenance enabled", {
        file,
        incidents,
        preview: previewChars,
      })

      return next
  }

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
