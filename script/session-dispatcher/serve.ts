// Source-only host for a dedicated dispatcher service. No managed-service election.
import { mkdirSync, readFileSync, realpathSync, writeFileSync, existsSync, renameSync } from "node:fs"
import { resolve, join } from "node:path"
import { randomBytes } from "node:crypto"

const argument = process.argv[2]
if (!argument || process.argv.length !== 3) throw new Error("Usage: bun --no-install serve.ts /absolute/private/state-root")
const root = resolve(argument)
mkdirSync(root, { recursive: true, mode: 0o700 })
if (realpathSync(root) !== root) throw new Error("Use a canonical state root")
const marker = join(root, "dispatcher-host.json")
const database = join(root, "data/opencode/opencode.db")
if (existsSync(database) && !existsSync(marker)) throw new Error("Refusing an existing unmanaged OpenCode database")
const config = join(root, "config/opencode/opencode.json")
const settings = JSON.parse(readFileSync(config, "utf8"))
if (settings.plugin?.length || settings.plugins?.length) throw new Error("This minimal host does not load plugins")
if (settings.warming !== false || settings.update !== "disable" || settings.share !== "disabled")
  throw new Error("Set warming:false, update:'disable', share:'disabled' in the dedicated config")

// These must precede dynamic imports: Global.Path captures XDG roots at import time.
for (const key of Object.keys(process.env)) {
  if (key.startsWith("OPENCODE_")) delete process.env[key]
}
process.env.OPENCODE_TEST_HOME = join(root, "home")
process.env.XDG_DATA_HOME = join(root, "data")
process.env.XDG_CONFIG_HOME = join(root, "config")
process.env.XDG_CACHE_HOME = join(root, "cache")
process.env.XDG_STATE_HOME = join(root, "state")
process.env.TMPDIR = join(root, "tmp")
mkdirSync(process.env.TMPDIR, { recursive: true, mode: 0o700 })
writeFileSync(marker, JSON.stringify({ kind: "manual-dispatcher-host", version: 1 }) + "\n", { mode: 0o600 })

const { NodeRuntime, NodeServices } = await import("../../packages/server/node_modules/@effect/platform-node/dist/index.js")
const { Effect } = await import("../../packages/server/node_modules/effect/dist/index.js")
const { Global } = await import("../../packages/util/src/global.ts")
const { AppProcess } = await import("../../packages/util/src/process.ts")
const { LayerNode } = await import("../../packages/util/src/effect/layer-node.ts")
const { start } = await import("../../packages/server/src/process.ts")

const program = Effect.scoped(Effect.gen(function* () {
  const password = randomBytes(32).toString("hex")
  const server = yield* start({
    app: { name: "manual-dispatcher", version: "source", channel: "dispatcher" },
    hostname: "127.0.0.1",
    port: 0,
    password,
    database: { path: database },
    events: { persist: true },
    models: { fetch: false },
    config: { directory: join(root, "config/opencode"), project: false, claude: false },
    fs: { fff: false },
  })
  if (server.address._tag !== "InetAddressV4") throw new Error("Expected IPv4 listener")
  const url = `http://127.0.0.1:${server.address.port}`
  const temporary = join(root, "connection.next.json")
  writeFileSync(temporary, JSON.stringify({ url, password }) + "\n", { mode: 0o600 })
  renameSync(temporary, join(root, "connection.json"))
  console.log(`Dedicated dispatcher host ready: ${url}; events.persist=true`)
  yield* server.shutdown
})).pipe(
  Effect.provide(LayerNode.compile(LayerNode.group([Global.node, AppProcess.node]))),
  Effect.provide(NodeServices.layer),
)

NodeRuntime.runMain(program)
