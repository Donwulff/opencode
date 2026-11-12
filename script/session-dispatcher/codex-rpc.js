// JSONL bridge to the existing Codex Unix WebSocket; no server or inference launch.
import WebSocket from "ws";
import { statSync, realpathSync } from "node:fs";
import { createInterface } from "node:readline";

const path = realpathSync(process.argv[2]);
const info = statSync(path);
const parent = statSync(path.slice(0, path.lastIndexOf("/")) || "/");
if (!path.startsWith("/") || /[:?#%\r\n]/.test(path) || !info.isSocket() ||
    info.uid !== process.getuid() || ((info.mode & 0o077) && (parent.uid !== process.getuid() || (parent.mode & 0o077)))) {
  throw new Error("Owned private Unix socket required");
}
const socket = new WebSocket(`ws+unix://${path}:/`);
const timer = setTimeout(() => { socket.terminate(); process.exit(1); }, 5000);
socket.on("error", () => process.exit(1));
socket.on("close", () => process.exit(0));
socket.on("message", (data) => {
  if (Buffer.byteLength(data) > 4 * 1024 * 1024) process.exit(1);
  process.stdout.write(String(data) + "\n");
});
await new Promise((resolve) => socket.once("open", resolve));
clearTimeout(timer);
for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  if (Buffer.byteLength(line) > 1024 * 1024) process.exit(1);
  JSON.parse(line);
  socket.send(line);
}
socket.close();
