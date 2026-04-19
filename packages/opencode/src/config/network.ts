import { Schema } from "effect"
import { zod } from "@/util/effect-zod"
import { withStatics } from "@/util/schema"

const Port = Schema.Number.check(Schema.isInt()).check(Schema.isGreaterThanOrEqualTo(1)).check(Schema.isLessThanOrEqualTo(65535))

export const Egress = Schema.Struct({
  default_policy: Schema.optional(Schema.Literals(["drop", "accept"])).annotate({
    description:
      "Default policy for packets that match no allow rule. 'drop' (default) denies everything not explicitly allowed; 'accept' allows everything not explicitly blocked. The container entrypoint compiles this into an nftables ruleset at startup.",
  }),
  allow_cidrs: Schema.optional(Schema.mutable(Schema.Array(Schema.String))).annotate({
    description:
      "IPv4/IPv6 addresses or CIDR ranges the container may reach. Names are not resolved here — commit resolved IPs from a trusted source. Example: ['10.0.0.0/8', '192.0.2.42'].",
  }),
  allow_ports: Schema.optional(Schema.mutable(Schema.Array(Port))).annotate({
    description:
      "TCP/UDP destination ports allowed for hosts in allow_cidrs. If omitted, common outbound ports (53, 80, 443) are used. Applied as a port allowlist on top of the CIDR allowlist.",
  }),
  allow_rules: Schema.optional(Schema.mutable(Schema.Array(Schema.String))).annotate({
    description:
      "Raw nftables rule fragments appended to the filter chain before the default policy. Escape hatch for cases the structured allowlist cannot express (e.g. protocol-specific matches). Use with care.",
  }),
}).pipe(withStatics((s) => ({ zod: zod(s) })))

export type Egress = Schema.Schema.Type<typeof Egress>

export const Info = Schema.Struct({
  egress: Schema.optional(Egress).annotate({
    description:
      "Container-level egress firewall policy. Consumed by the analysis-container entrypoint (not the opencode process) to compile an nftables ruleset before dropping privileges. See docker/NETWORK-ARCHITECTURE.md.",
  }),
}).pipe(withStatics((s) => ({ zod: zod(s) })))

export type Info = Schema.Schema.Type<typeof Info>

export * as ConfigNetwork from "./network"
