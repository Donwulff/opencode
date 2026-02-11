import { Log } from "./log"
import path from "path"
import { appendFile } from "node:fs/promises"
import { Global } from "../global"

const log = Log.create({ service: "audit" })

export interface AuditLogEntry {
  timestamp: string
  type:
    | "url_check"
    | "provider_load"
    | "model_access"
    | "blocked_access"
    | "fetch_request"
    | "session_model_change"
    | "tool_request"
    | "tool_blocked"
    | "sub_agent_invocation"
  level: "info" | "warn" | "error"
  message: string
  context?: Record<string, string>
  metadata?: Record<string, any>
}

export class AuditLogger {
  private entries: AuditLogEntry[] = []
  private filePath: string
  enabled: boolean

  constructor(enabled: boolean = true, filePath?: string) {
    this.enabled = enabled
    this.filePath = filePath ?? path.join(Global.Path.data, "audit.jsonl")
  }

  private log(
    type: AuditLogEntry["type"],
    message: string,
    level: AuditLogEntry["level"] = "info",
    context?: Record<string, string>,
    metadata?: Record<string, any>,
  ): void {
    if (!this.enabled) return

    const entry: AuditLogEntry = {
      timestamp: new Date().toISOString(),
      type,
      level,
      message,
      context,
      metadata,
    }

    this.entries.push(entry)
    void this.save()
  }

  logUrlCheck(url: string, allowed: boolean, reason: string, context?: Record<string, string>): void {
    const level = allowed ? "info" : "warn"
    const type = allowed ? "url_check" : "blocked_access"
    this.log(type, reason, level, { url, ...context }, { allowed })
  }

  logProviderLoad(
    providerId: string,
    source: "env" | "config" | "api" | "custom",
    modelsCount: number,
    allowed: boolean,
    reason?: string,
  ): void {
    const level = allowed ? "info" : "warn"
    const message = allowed
      ? `Provider loaded: ${providerId} (${source}, ${modelsCount} models)`
      : `Provider blocked: ${providerId}`
    this.log("provider_load", message, level, { providerId, source }, { modelsCount, allowed })
  }

  logModelAccess(providerId: string, modelId: string, allowed: boolean, reason?: string): void {
    const level = allowed ? "info" : "warn"
    const message = allowed
      ? `Model accessed: ${providerId}/${modelId}`
      : `Model access blocked: ${providerId}/${modelId}`
    this.log("model_access", message, level, { providerId, modelId }, { allowed })
  }

  logSessionModelChange(
    sessionId: string,
    providerId: string,
    modelId: string,
    allowed: boolean,
    reason?: string,
  ): void {
    const level = allowed ? "info" : "warn"
    const message = allowed
      ? `Session ${sessionId} model set: ${providerId}/${modelId}`
      : `Session ${sessionId} provider change blocked: ${providerId}/${modelId}`
    this.log(
      "session_model_change",
      message,
      level,
      {
        sessionId,
        providerId,
        modelId,
      },
      { allowed, reason },
    )
  }

  logToolRequest(
    sessionId: string,
    toolName: string,
    destination: string,
    providerId: string,
    allowed: boolean,
    reason?: string,
  ): void {
    const level = allowed ? "info" : "warn"
    const type = allowed ? "tool_request" : "tool_blocked"
    const message = allowed
      ? `Tool ${toolName} allowed to ${destination}`
      : `Tool ${toolName} blocked from ${destination}`
    this.log(
      type,
      message,
      level,
      {
        sessionId,
        toolName,
        destination,
        providerId,
      },
      { allowed },
    )
  }

  logSubAgentInvocation(
    parentSessionId: string,
    subAgentId: string,
    providerId: string,
    allowed: boolean,
    reason?: string,
  ): void {
    const level = allowed ? "info" : "warn"
    const message = allowed
      ? `Sub-agent ${subAgentId} invoked with ${providerId}`
      : `Sub-agent ${subAgentId} blocked from ${providerId}`
    this.log(
      "sub_agent_invocation",
      message,
      level,
      {
        parentSessionId,
        subAgentId,
        providerId,
      },
      { allowed, reason },
    )
  }

  logFetchRequest(
    providerId: string,
    method: string,
    url: string,
    status: number,
    durationMs: number,
    reason?: string,
  ): void {
    this.log(
      "fetch_request",
      `HTTP ${method} ${url}`,
      "info",
      {
        providerId,
        status: String(status),
        duration: `${durationMs}ms`,
      },
      { method, status, durationMs, reason },
    )
  }

  logBlockedAccess(url: string, reason: string, source: string): void {
    this.log("blocked_access", reason, "warn", { url, source })
  }

  getEntries(): AuditLogEntry[] {
    return this.entries
  }

  async save(): Promise<void> {
    if (!this.enabled) return
    try {
      const content = this.entries.map((entry) => JSON.stringify(entry) + "\n").join("")
      await appendFile(this.filePath, content)
      log.info("Audit log entries saved", { path: this.filePath, count: this.entries.length })
    } catch (error) {
      log.error("Failed to save audit log", { error })
    }
    this.entries = []
  }

  clear(): void {
    this.entries = []
    log.info("Audit log cleared")
  }

  getFiltered(
    filters: {
      type?: AuditLogEntry["type"]
      providerId?: string
      allowed?: boolean
    } = {},
  ): AuditLogEntry[] {
    return this.entries.filter((entry) => {
      if (filters.type && entry.type !== filters.type) return false
      if (filters.providerId) {
        const contextProvider = entry.context?.providerId || entry.metadata?.providerId
        if (contextProvider !== filters.providerId) return false
      }
      if (filters.allowed !== undefined) {
        const isAllowed = entry.metadata?.allowed ?? true
        if (isAllowed !== filters.allowed) return false
      }
      return true
    })
  }
}

export const auditLogger = new AuditLogger()

export function setAuditLogEnabled(enabled: boolean): void {
  auditLogger.enabled = enabled
}
