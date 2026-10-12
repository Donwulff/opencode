#!/usr/bin/env bun

// A launcher paused at the merge prompt still holds the previous build and binary paths.
import path from "path"
import { mkdir, rm, symlink } from "fs/promises"

const root = path.resolve(import.meta.dirname, "../../..")
const cached = path.join(root, ".scripts/runtime/bun")
const runtime = (await Bun.file(cached).exists()) ? cached : process.execPath
const args = process.argv.slice(2)
const result = await Bun.spawn({
  cmd: [
    runtime,
    path.join(root, "packages/cli/script/build.ts"),
    ...args,
    ...(args.includes("--single") && !args.includes("--skip-install") ? ["--skip-install"] : []),
  ],
  cwd: root,
  env: { ...process.env, PATH: `${path.dirname(runtime)}${path.delimiter}${process.env.PATH ?? ""}` },
  stdout: "inherit",
  stderr: "inherit",
}).exited
if (result !== 0) process.exit(result)

const target = `${process.platform}-${process.arch}`
const binary = process.platform === "win32" ? "opencode.exe" : "opencode"
const legacy = path.join(root, "packages/opencode/dist", `opencode-${target}`, "bin", binary)
await mkdir(path.dirname(legacy), { recursive: true })
await rm(legacy, { force: true })
await symlink(path.join(root, "packages/cli/dist", `cli-${target}`, "bin", binary), legacy)
