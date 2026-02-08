/// <reference path="../env.d.ts" />
import { tool } from "@opencode-ai/plugin"
import DESCRIPTION from "./llama-slot.txt"

const actions = ["list", "save", "restore", "erase"] as const

function normalize(url: string) {
  const trimmed = url.trim().replace(/\/+$/, "")
  if (trimmed.endsWith("/v1")) return trimmed.slice(0, -3)
  return trimmed
}

async function request(url: string, init?: RequestInit) {
  const response = await fetch(url, init)
  if (!response.ok) {
    throw new Error(`llama.cpp API error: ${response.status} ${response.statusText}`)
  }
  const type = response.headers.get("content-type") || ""
  if (type.includes("application/json")) {
    return JSON.stringify(await response.json(), null, 2)
  }
  return response.text()
}

export default tool({
  description: DESCRIPTION,
  args: {
    base_url: tool.schema
      .string()
      .optional()
      .describe("llama-server base URL (no /v1). Falls back to LLAMA_SLOT_URL env var."),
    action: tool.schema.enum(actions).default("list").describe("Slot action to perform"),
    slot: tool.schema.number().int().nonnegative().optional().describe("Slot id for save/restore/erase"),
    filename: tool.schema.string().optional().describe("Filename for save/restore (relative to --slot-save-path)"),
    fail_on_no_slot: tool.schema
      .boolean()
      .optional()
      .describe("For list, return 503 when all slots are busy"),
  },
  async execute(args) {
    const base = normalize(args.base_url ?? process.env.LLAMA_SLOT_URL ?? "")
    if (!base) {
      throw new Error("base_url is required (or set LLAMA_SLOT_URL)")
    }

    if (args.action === "list") {
      const suffix = args.fail_on_no_slot ? "?fail_on_no_slot=1" : ""
      return request(`${base}/slots${suffix}`)
    }

    if (args.slot === undefined) {
      throw new Error("slot is required for save/restore/erase")
    }

    const url = `${base}/slots/${args.slot}?action=${args.action}`
    if (args.action === "erase") {
      return request(url, { method: "POST" })
    }

    const filename = args.filename ?? `slot_${args.slot}.bin`
    return request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename }),
    })
  },
})
