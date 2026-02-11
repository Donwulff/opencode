import type { Tool } from "./tool"
import path from "path"

function cmd(ctx: Tool.Context) {
  return typeof ctx.extra?.command === "string" ? ctx.extra.command : undefined
}

export function assertLearnPath(ctx: Tool.Context, filePath: string) {
  const kind = cmd(ctx)
  if (kind === "review") {
    throw new Error("The /review command is read-only and cannot modify files.")
  }
  if (kind !== "learn") return
  if (path.basename(filePath) === "AGENTS.md") return
  throw new Error("The /learn command may only modify AGENTS.md files.")
}

export function assertLearnTask(ctx: Tool.Context) {
  if (cmd(ctx) !== "learn") return
  throw new Error("The /learn command cannot launch sub-agents via task tool.")
}

export function assertReadOnlyShell(ctx: Tool.Context, command: string) {
  const kind = cmd(ctx)
  if (!kind) return
  if (kind !== "learn" && kind !== "review") return

  const input = command.trim()
  if (!input) return

  if (/(^|[^<])>>?|<<|<\(/.test(input)) {
    throw new Error(`The /${kind} command is read-only and cannot use shell redirection.`)
  }

  if (
    /(^|[;&|]\s*)git\s+(reset|checkout|restore|stash|clean|rebase|merge|commit|push|cherry-pick|revert|switch|branch|tag)\b/.test(
      input,
    )
  ) {
    throw new Error(`The /${kind} command is read-only and cannot run mutating git commands.`)
  }

  if (/(^|[;&|]\s*)(rm|mv|cp|chmod|chown|touch|mkdir|rmdir)\b/.test(input)) {
    throw new Error(`The /${kind} command is read-only and cannot run file-mutating shell commands.`)
  }
}
