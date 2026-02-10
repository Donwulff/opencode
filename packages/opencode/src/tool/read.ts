import z from "zod"
import * as fs from "fs"
import * as path from "path"
import { Tool } from "./tool"
import { LSP } from "../lsp"
import { FileTime } from "../file/time"
import DESCRIPTION from "./read.txt"
import { Instance } from "../project/instance"
import { Identifier } from "../id/id"
import { assertExternalDirectory } from "./external-directory"
import { InstructionPrompt } from "../session/instruction"

const DEFAULT_READ_LIMIT = 2000
const MAX_LINE_LENGTH = 2000
const MAX_BYTES = 50 * 1024
const seen = new Map<
  string,
  { requested: number; limit: number; truncated: boolean; cursor: number; explicitOffset: boolean }
>()

export const ReadTool = Tool.define("read", {
  description: DESCRIPTION,
  parameters: z.object({
    filePath: z.string().describe("The path to the file to read"),
    offset: z.coerce.number().describe("The line number to start reading from (0-based)").optional(),
    limit: z.coerce.number().describe("The number of lines to read (defaults to 2000)").optional(),
  }),
  async execute(params, ctx) {
    let filepath = params.filePath
    if (!path.isAbsolute(filepath)) {
      filepath = path.resolve(Instance.directory, filepath)
    }
    const title = path.relative(Instance.worktree, filepath)

    await assertExternalDirectory(ctx, filepath, {
      bypass: Boolean(ctx.extra?.["bypassCwdCheck"]),
    })

    await ctx.ask({
      permission: "read",
      patterns: [filepath],
      always: ["*"],
      metadata: {},
    })

    const file = Bun.file(filepath)
    if (!(await file.exists())) {
      const dir = path.dirname(filepath)
      const base = path.basename(filepath)

      const dirEntries = fs.readdirSync(dir)
      const suggestions = dirEntries
        .filter(
          (entry) =>
            entry.toLowerCase().includes(base.toLowerCase()) || base.toLowerCase().includes(entry.toLowerCase()),
        )
        .map((entry) => path.join(dir, entry))
        .slice(0, 3)

      if (suggestions.length > 0) {
        throw new Error(`File not found: ${filepath}\n\nDid you mean one of these?\n${suggestions.join("\n")}`)
      }

      throw new Error(`File not found: ${filepath}`)
    }

    const instructions = await InstructionPrompt.resolve(ctx.messages, filepath, ctx.messageID)

    // Exclude SVG (XML-based) and vnd.fastbidsheet (.fbs extension, commonly FlatBuffers schema files)
    const isImage =
      file.type.startsWith("image/") && file.type !== "image/svg+xml" && file.type !== "image/vnd.fastbidsheet"
    const isPdf = file.type === "application/pdf"
    if (isImage || isPdf) {
      const mime = file.type
      const msg = `${isImage ? "Image" : "PDF"} read successfully`
      return {
        title,
        output: msg,
        metadata: {
          preview: msg,
          truncated: false,
          ...(instructions.length > 0 && { loaded: instructions.map((i) => i.filepath) }),
        },
        attachments: [
          {
            id: Identifier.ascending("part"),
            sessionID: ctx.sessionID,
            messageID: ctx.messageID,
            type: "file",
            mime,
            url: `data:${mime};base64,${Buffer.from(await file.bytes()).toString("base64")}`,
          },
        ],
      }
    }

    const isBinary = await isBinaryFile(filepath, file)
    if (isBinary) throw new Error(`Cannot read binary file: ${filepath}`)

    const limit = params.limit ?? DEFAULT_READ_LIMIT
    const hasOffset = params.offset !== undefined
    const offset = hasOffset ? params.offset : 0
    const key = `${ctx.sessionID}:${filepath}`
    const prev = seen.get(key)
    const lines = await file.text().then((text) => text.split("\n"))

    const slice = (start: number) => {
      const raw: string[] = []
      let bytes = 0
      let truncatedByBytes = false
      for (let i = start; i < Math.min(lines.length, start + limit); i++) {
        const line = lines[i].length > MAX_LINE_LENGTH ? lines[i].substring(0, MAX_LINE_LENGTH) + "..." : lines[i]
        const size = Buffer.byteLength(line, "utf-8") + (raw.length > 0 ? 1 : 0)
        if (bytes + size > MAX_BYTES) {
          truncatedByBytes = true
          break
        }
        raw.push(line)
        bytes += size
      }
      const lastReadLine = start + raw.length
      const totalLines = lines.length
      const hasMoreLines = totalLines > lastReadLine
      const truncated = hasMoreLines || truncatedByBytes
      return {
        raw,
        truncatedByBytes,
        lastReadLine,
        totalLines,
        hasMoreLines,
        truncated,
      }
    }

    const first = slice(offset)
    const repeat = !hasOffset && prev && prev.truncated && !prev.explicitOffset && first.truncated
    const start = repeat ? prev.cursor : offset
    const result = repeat ? slice(start) : first
    const content = result.raw.map((line, index) => {
      return `${(index + start + 1).toString().padStart(5, "0")}| ${line}`
    })
    const preview = result.raw.slice(0, 20).join("\n")

    const lineStart = start + 1
    const lineEnd = start + Math.max(result.raw.length, 1)
    let output = `<file-header>\nLines ${lineStart}-${lineEnd} (1-based, absolute). Use offset=(line-1) to read a specific line.\n</file-header>\n<file>\n`
    output += content.join("\n")
    output += "\n</file>"

    const status = result.truncatedByBytes
      ? `Output truncated at ${MAX_BYTES} bytes. Use 'offset' parameter to read beyond line ${result.lastReadLine}.`
      : result.hasMoreLines
        ? `File has more lines. Use 'offset' parameter to read beyond line ${result.lastReadLine}.`
        : `End of file - total ${result.totalLines} lines.`
    const next = result.truncated ? `Next read: set offset to ${result.lastReadLine} (0-based).` : ""
    const advanced = repeat ? `SYSTEM NOTICE: Offset not given; continuing from offset ${start} (0-based).` : ""

    output += `\n\n(${status})`
    if (next) output += `\n${next}`
    if (advanced) output += `\n${advanced}`

    // just warms the lsp client
    LSP.touchFile(filepath, false)
    FileTime.read(ctx.sessionID, filepath)

    if (instructions.length > 0) {
      output += `\n\n<system-reminder>\n${instructions.map((i) => i.content).join("\n\n")}\n</system-reminder>`
    }

    seen.set(key, {
      requested: offset,
      limit,
      truncated: result.truncated,
      cursor: result.lastReadLine,
      explicitOffset: hasOffset,
    })

    return {
      title,
      output,
      metadata: {
        preview,
        truncated: result.truncated,
        ...(instructions.length > 0 && { loaded: instructions.map((i) => i.filepath) }),
      },
    }
  },
})

async function isBinaryFile(filepath: string, file: Bun.BunFile): Promise<boolean> {
  const ext = path.extname(filepath).toLowerCase()
  // binary check for common non-text extensions
  switch (ext) {
    case ".zip":
    case ".tar":
    case ".gz":
    case ".exe":
    case ".dll":
    case ".so":
    case ".class":
    case ".jar":
    case ".war":
    case ".7z":
    case ".doc":
    case ".docx":
    case ".xls":
    case ".xlsx":
    case ".ppt":
    case ".pptx":
    case ".odt":
    case ".ods":
    case ".odp":
    case ".bin":
    case ".dat":
    case ".obj":
    case ".o":
    case ".a":
    case ".lib":
    case ".wasm":
    case ".pyc":
    case ".pyo":
      return true
    default:
      break
  }

  const stat = await file.stat()
  const fileSize = stat.size
  if (fileSize === 0) return false

  const bufferSize = Math.min(4096, fileSize)
  const buffer = await file.arrayBuffer()
  if (buffer.byteLength === 0) return false
  const bytes = new Uint8Array(buffer.slice(0, bufferSize))

  let nonPrintableCount = 0
  for (let i = 0; i < bytes.length; i++) {
    if (bytes[i] === 0) return true
    if (bytes[i] < 9 || (bytes[i] > 13 && bytes[i] < 32)) {
      nonPrintableCount++
    }
  }
  // If >30% non-printable characters, consider it binary
  return nonPrintableCount / bytes.length > 0.3
}
