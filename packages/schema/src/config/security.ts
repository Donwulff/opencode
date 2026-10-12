export * as ConfigSecurity from "./security.js"

import { Schema } from "effect"
import { optional } from "../schema.js"

export const Info = Schema.Struct({
  mode: Schema.Literals(["internal-only", "external-allowed", "strict"]).pipe(optional),
  allow_internal_dns_suffixes: Schema.Array(Schema.String).pipe(optional),
  block_domain_regex: Schema.Array(Schema.String).pipe(optional),
  allow_local: Schema.Boolean.pipe(optional),
  allow_private_ip: Schema.Boolean.pipe(optional),
  models_dev_enabled: Schema.Boolean.pipe(optional),
  audit_log_enabled: Schema.Boolean.pipe(optional),
  external_domains: Schema.Array(Schema.String).pipe(optional),
  allow_external_ips: Schema.Boolean.pipe(optional),
  allowed_ip_ranges: Schema.Array(Schema.String).pipe(optional),
}).annotate({ identifier: "Config.Security" })
export interface Info extends Schema.Schema.Type<typeof Info> {}
