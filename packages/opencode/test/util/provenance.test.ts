import { describe, expect, it } from "bun:test"
import path from "node:path"
import { Global } from "@opencode-ai/core/global"
import { Provenance } from "@/provenance"

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
