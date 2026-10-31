export * as CodeModeExecution from "./execution.js"

import type { Context as ToolContext } from "@opencode/schema/tool"
import { Context } from "effect"
import type { Config } from "../config.js"

/** The `execute` call a Code Mode program runs under, so extensions can act for its session like a tool does. */
export class Service extends Context.Service<Service, ToolContext & { readonly config?: Config.Interface }>()(
  "@opencode/CodeModeExecution",
) {}
