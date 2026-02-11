import type { MessageV2 } from "@/session/message-v2"
import type { Tool } from "./tool"
import path from "path"

export function activeCommand(messages: MessageV2.WithParts[]) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const info = messages[i].info
    if (info.role !== "user") continue
    if (info.command) return info.command
  }
}

export function assertLearnPath(ctx: Tool.Context, filePath: string) {
  if (ctx.extra?.command !== "learn") return
  if (path.basename(filePath) === "AGENTS.md") return
  throw new Error("The /learn command may only modify AGENTS.md files.")
}
