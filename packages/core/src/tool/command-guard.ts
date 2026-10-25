export * as CommandGuard from "./command-guard.js"

import path from "path"
import { ToolFailure } from "@opencode/ai/effect"
import type { Session } from "@opencode/schema/session"
import { Effect, Option } from "effect"
import { SessionStore } from "../session/store.js"

export const make = Effect.fn("CommandGuard.make")(function* () {
  const store = Option.getOrUndefined(yield* Effect.serviceOption(SessionStore.Service))
  const command = Effect.fnUntraced(function* (sessionID: Session.ID) {
    if (!store) return
    const messages = yield* store.messages({ sessionID, type: "user", order: "desc", limit: 1 }).pipe(Effect.orDie)
    const message = messages[0]
    return message?.type === "user" ? message.command : undefined
  })
  return {
    path: Effect.fn("CommandGuard.path")(function* (sessionID: Session.ID, filePath: string) {
      const kind = yield* command(sessionID)
      if (kind === "review")
        return yield* new ToolFailure({ message: "The /review command is read-only and cannot modify files." })
      if (kind === "learn" && path.basename(filePath) !== "AGENTS.md")
        return yield* new ToolFailure({ message: "The /learn command may only modify AGENTS.md files." })
    }),
    subagent: Effect.fn("CommandGuard.subagent")(function* (sessionID: Session.ID) {
      if ((yield* command(sessionID)) === "learn")
        return yield* new ToolFailure({ message: "The /learn command cannot launch sub-agents." })
    }),
    shell: Effect.fn("CommandGuard.shell")(function* (sessionID: Session.ID, input: string) {
      const kind = yield* command(sessionID)
      if (kind !== "learn" && kind !== "review") return
      if (/(^|[^<])>>?|<<|<\(/.test(input))
        return yield* new ToolFailure({
          message: `The /${kind} command is read-only and cannot use shell redirection.`,
        })
      if (
        /(^|[;&|]\s*)git\s+(reset|checkout|restore|stash|clean|rebase|merge|commit|push|cherry-pick|revert|switch|branch|tag)\b/.test(
          input,
        ) ||
        /(^|[;&|]\s*)(rm|mv|cp|chmod|chown|touch|mkdir|rmdir)\b/.test(input)
      )
        return yield* new ToolFailure({
          message: `The /${kind} command is read-only and cannot run mutating shell commands.`,
        })
    }),
  }
})
