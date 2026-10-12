export * as ShellSandbox from "./sandbox.js"

import { Effect } from "effect"
import { which } from "../util/which.js"

export const command = Effect.fn("ShellSandbox.command")(function* (shell: string, args: string[], command: string) {
  const flag = process.env.OPENCODE_SPAWN_SANDBOX?.toLowerCase()
  if (process.platform !== "linux" || (flag !== "1" && flag !== "true")) return { shell, args }
  const inner = `ip link set lo up 2>/dev/null; ${command}`
  const bwrap = which("bwrap")
  if (bwrap)
    return {
      shell: bwrap,
      args: [
        "--bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--unshare-user",
        "--uid",
        "0",
        "--gid",
        "0",
        "--unshare-net",
        "--",
        shell,
        "-c",
        inner,
      ],
    }
  const unshare = which("unshare")
  if (unshare) return { shell: unshare, args: ["--user", "--map-root-user", "--net", shell, "-c", inner] }
  yield* Effect.logWarning("OPENCODE_SPAWN_SANDBOX set but neither bwrap nor unshare found; running unsandboxed")
  return { shell, args }
})
