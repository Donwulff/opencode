import type { Tool } from "./tool"
import path from "path"

export function assertLearnPath(ctx: Tool.Context, filePath: string) {
  if (ctx.extra?.command !== "learn") return
  if (path.basename(filePath) === "AGENTS.md") return
  throw new Error("The /learn command may only modify AGENTS.md files.")
}
