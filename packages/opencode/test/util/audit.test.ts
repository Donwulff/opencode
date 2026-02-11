import { describe, it, expect, beforeEach } from "bun:test"
import { AuditLogger, setAuditLogEnabled } from "@/util/audit"

describe("AuditLogger", () => {
  let tmpDir: string

  beforeEach(() => {
    // Create temp directory for audit files
    tmpDir = "/tmp/opencode-test-audit"
  })

  it("creates an audit logger", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    expect(logger).toBeInstanceOf(AuditLogger)
    expect(logger).toBeDefined()
  })

  it("logs URL checks", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logUrlCheck("http://localhost:8080", true, "URL validated", { providerId: "test" })

    const entries = logger.getEntries()
    expect(entries).toHaveLength(1)
    expect(entries[0].type).toBe("url_check")
    expect(entries[0].context?.url).toBe("http://localhost:8080")
  })

  it("logs blocked access", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logUrlCheck("https://api.openai.com", false, "External URL blocked", { providerId: "test" })

    const entries = logger.getEntries()
    expect(entries).toHaveLength(1)
    expect(entries[0].type).toBe("blocked_access")
    expect(entries[0].level).toBe("warn")
  })

  it("logs provider load", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logProviderLoad("openai", "config", 50, true, "Provider loaded")

    const entries = logger.getEntries()
    expect(entries).toHaveLength(1)
    expect(entries[0].type).toBe("provider_load")
    expect(entries[0].context?.providerId).toBe("openai")
    expect(entries[0].metadata?.modelsCount).toBe(50)
  })

  it("logs model access", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logModelAccess("openai", "gpt-4", true, "Model accessed")

    const entries = logger.getEntries()
    expect(entries).toHaveLength(1)
    expect(entries[0].type).toBe("model_access")
    expect(entries[0].context?.modelId).toBe("gpt-4")
  })

  it("logs session model change", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logSessionModelChange("session123", "openai", "gpt-4", true, "Model set")

    const entries = logger.getEntries()
    expect(entries).toHaveLength(1)
    expect(entries[0].type).toBe("session_model_change")
    expect(entries[0].context?.sessionId).toBe("session123")
  })

  it("logs tool request", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logToolRequest("session123", "webfetch", "https://example.com", "openai", true, "URL validated")

    const entries = logger.getEntries()
    expect(entries).toHaveLength(1)
    expect(entries[0].type).toBe("tool_request")
    expect(entries[0].context?.destination).toBe("https://example.com")
  })

  it("logs blocked tool request", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logToolRequest("session123", "webfetch", "https://malware.com", "openai", false, "URL blocked")

    const entries = logger.getEntries()
    expect(entries).toHaveLength(1)
    expect(entries[0].type).toBe("tool_blocked")
    expect(entries[0].level).toBe("warn")
  })

  it("logs fetch request", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logFetchRequest("openai", "POST", "https://api.openai.com/v1/chat/completions", 200, 450)

    const entries = logger.getEntries()
    expect(entries).toHaveLength(1)
    expect(entries[0].type).toBe("fetch_request")
    expect(entries[0].context?.status).toBe("200")
  })

  it("logs sub-agent invocation", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logSubAgentInvocation("session123", "subagent456", "openai", true, "Invoked")

    const entries = logger.getEntries()
    expect(entries).toHaveLength(1)
    expect(entries[0].type).toBe("sub_agent_invocation")
  })

  it("filters entries by type", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logUrlCheck("http://localhost", true, "URL validated")
    logger.logProviderLoad("openai", "config", 50, true)
    logger.logToolRequest("session123", "webfetch", "https://example.com", "openai", true)

    const blockedEntries = logger.getFiltered({ type: "blocked_access" })
    expect(blockedEntries).toHaveLength(0)

    const urlCheckEntries = logger.getFiltered({ type: "url_check" })
    expect(urlCheckEntries).toHaveLength(1)

    const toolEntries = logger.getFiltered({ type: "tool_request" })
    expect(toolEntries).toHaveLength(1)
  })

  it("filters entries by provider ID", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logProviderLoad("openai", "config", 50, true)
    logger.logProviderLoad("anthropic", "config", 30, true)

    const openaiEntries = logger.getFiltered({ providerId: "openai" })
    expect(openaiEntries).toHaveLength(1)

    const allEntries = logger.getFiltered({ providerId: "openai" })
    expect(allEntries).toHaveLength(1)
  })

  it("filters entries by allowed status", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logUrlCheck("http://localhost", true, "Allowed")
    logger.logUrlCheck("https://api.openai.com", false, "Blocked")

    const allowedEntries = logger.getFiltered({ allowed: true })
    expect(allowedEntries).toHaveLength(1)

    const blockedEntries = logger.getFiltered({ allowed: false })
    expect(blockedEntries).toHaveLength(1)
  })

  it("clears entries", () => {
    const logger = new AuditLogger(true, `${tmpDir}/audit.json`)
    logger.logUrlCheck("http://localhost", true, "URL validated")
    logger.clear()

    const entries = logger.getEntries()
    expect(entries).toHaveLength(0)
  })

  it("can be disabled", () => {
    const logger = new AuditLogger(false, `${tmpDir}/audit.json`)
    logger.logUrlCheck("http://localhost", true, "URL validated")

    const entries = logger.getEntries()
    expect(entries).toHaveLength(0)
  })

  it("uses custom filePath when provided", () => {
    const customPath = `${tmpDir}/custom-audit.json`
    const logger = new AuditLogger(true, customPath)
    logger.logUrlCheck("http://localhost", true, "URL validated")

    expect(logger.getEntries()).toHaveLength(1)
  })
})
