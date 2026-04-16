import { isIP, isIPv4, isIPv6 } from "net"
import * as Log from "./log"
import z from "zod"

const log = Log.create({ service: "network-validation" })

export type IPRange = {
  start: number[]
  end: number[]
}

export function parseUrl(url: string): URL {
  try {
    return new URL(url)
  } catch {
    throw new Error(`Invalid URL: ${url}`)
  }
}

export function extractHost(urlOrHost: string): string {
  try {
    const hostname = new URL(urlOrHost).hostname
    const bare = hostname.startsWith("[") && hostname.endsWith("]") ? hostname.slice(1, -1) : hostname
    return normalizeIPv4MappedIPv6(bare)
  } catch {
    return normalizeIPv4MappedIPv6(urlOrHost.split("/")[0].split(":")[0])
  }
}

/**
 * Normalize IPv4-mapped IPv6 addresses (e.g. ::ffff:7f00:1 or ::ffff:127.0.0.1)
 * to their plain IPv4 equivalent, so downstream checks like isLocalhost and
 * isPrivateIP work correctly.
 */
export function normalizeIPv4MappedIPv6(host: string): string {
  const lower = host.toLowerCase()

  // Handle dotted form: ::ffff:192.168.1.1
  const dottedMatch = /^::ffff:(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$/.exec(lower)
  if (dottedMatch) return dottedMatch[1]

  // Handle hex form: ::ffff:7f00:1 (which is how URL parser normalizes ::ffff:127.0.0.1)
  const hexMatch = /^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/.exec(lower)
  if (hexMatch) {
    const hi = parseInt(hexMatch[1], 16)
    const lo = parseInt(hexMatch[2], 16)
    return `${(hi >> 8) & 0xff}.${hi & 0xff}.${(lo >> 8) & 0xff}.${lo & 0xff}`
  }

  return host
}

export function isLocalhost(host: string): boolean {
  const normalized = normalizeIPv4MappedIPv6(host)
  return ["localhost", "127.0.0.1", "::1", "0.0.0.0"].includes(normalized)
}

export function isPrivateIP(host: string): boolean {
  const normalized = normalizeIPv4MappedIPv6(host)

  if (!isIP(normalized)) return false

  const ipType = isIPv4(normalized) ? 4 : isIPv6(normalized) ? 6 : 0
  if (ipType === 0) return false

  if (ipType === 4) {
    const parts = normalized.split(".").map(Number)
    if (parts[0] === 10) return true // 10.0.0.0/8
    if (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) return true // 172.16.0.0/12
    if (parts[0] === 192 && parts[1] === 168) return true // 192.168.0.0/16
    if (parts[0] === 127) return true // 127.0.0.0/8
    if (parts[0] === 169 && parts[1] === 254) return true // 169.254.0.0/16 (link-local / cloud metadata)
    if (parts[0] === 0) return true // 0.0.0.0/8
    if (parts[0] === 100 && parts[1] >= 64 && parts[1] <= 127) return true // 100.64.0.0/10 (carrier-grade NAT)
  }

  if (ipType === 6) {
    if (normalized === "::1") return true // loopback
    if (normalized === "::") return true // unspecified
    // fe80::/10 (link-local)
    if (isCIDRMatch(normalized, "fe80::/10")) return true
    // fc00::/7 (unique local)
    if (isCIDRMatch(normalized, "fc00::/7")) return true
  }

  return false
}

function expandIPv6(addr: string): bigint[] {
  const parts = addr.split(":")
  const doubleColonIndex = parts.indexOf("")
  if (doubleColonIndex === -1) {
    return parts.map((p) => BigInt(`0x${p}`))
  }
  const omitted = 8 - (parts.length - 1)
  const left = doubleColonIndex
  const expanded = [
    ...parts.slice(0, left),
    ...Array.from({ length: omitted }, () => BigInt(0)),
    ...parts.slice(doubleColonIndex + 1),
  ]
  return expanded.map((p) => (p === "" ? BigInt(0) : BigInt(`0x${p}`)))
}

export function isCIDRMatch(host: string, cidr: string): boolean {
  if (!isIP(host)) return false

  const [network, prefixLen] = cidr.split("/")
  const prefix = Number(prefixLen)

  if (isNaN(prefix) || prefix < 0) return false

  if (prefix === 0) return true

  if (isIPv4(host) && isIPv4(network)) {
    const hostParts = host.split(".").map(Number)
    const networkParts = network.split(".").map(Number)

    const mask = ~((1 << (32 - prefix)) - 1)

    const hostNum = (hostParts[0] << 24) + (hostParts[1] << 16) + (hostParts[2] << 8) + hostParts[3]
    const networkNum = (networkParts[0] << 24) + (networkParts[1] << 16) + (networkParts[2] << 8) + networkParts[3]

    return (hostNum & mask) === (networkNum & mask)
  }

  if (isIPv6(host) && isIPv6(network)) {
    const hostParts = expandIPv6(host)
    const networkParts = expandIPv6(network)

    const hostBigInt = hostParts.reduce((acc, part) => (acc << BigInt(16)) + part, BigInt(0))
    const networkBigInt = networkParts.reduce((acc, part) => (acc << BigInt(16)) + part, BigInt(0))

    const bitsInMask = Math.min(128, prefix)
    const bitsInLastPart = bitsInMask % 16
    const fullParts = Math.floor(bitsInMask / 16)

    const maskBigInt = (() => {
      let mask = BigInt(0)
      for (let i = 0; i < fullParts; i++) {
        mask = (mask << BigInt(16)) + BigInt("0xFFFF")
      }
      if (bitsInLastPart > 0) {
        const partPattern = (BigInt(1) << BigInt(bitsInLastPart)) - BigInt(1)
        mask = (mask << BigInt(16)) + (partPattern << BigInt(16 - bitsInLastPart))
      }
      for (let i = fullParts + (bitsInLastPart > 0 ? 1 : 0); i < 8; i++) {
        mask = (mask << BigInt(16)) + BigInt(0)
      }
      return mask
    })()

    return (hostBigInt & maskBigInt) === (networkBigInt & maskBigInt)
  }

  return false
}

export function isInternalDNS(host: string, suffixes: string[]): boolean {
  return suffixes.some((suffix) => {
    const bare = suffix.replace(/^\./, "")
    if (host === bare) return true
    const dotSuffix = "." + bare
    return host.endsWith(dotSuffix)
  })
}

export function matchesPattern(host: string, pattern: string): boolean {
  const regex = pattern.replace(/\./g, "\\.").replace(/\*/g, ".*")
  try {
    return new RegExp(`^${regex}$`).test(host)
  } catch {
    return false
  }
}

export interface ValidationResult {
  allowed: boolean
  reason: string
}

export const SecurityConfig = z
  .object({
    mode: z.enum(["internal-only", "external-allowed", "strict"]).optional().describe("Security mode"),
    allow_internal_dns_suffixes: z.string().array().optional().describe("Internal DNS suffixes to allow"),
    block_domain_regex: z.string().array().optional().describe("Regular expressions to match and block domains"),
    allow_local: z.boolean().optional().default(true).describe("Allow localhost URLs"),
    allow_private_ip: z.boolean().optional().default(true).describe("Allow private IP addresses"),
    models_dev_enabled: z.boolean().optional().default(true).describe("Enable fetching models from models.dev"),
    audit_log_enabled: z.boolean().optional().default(true).describe("Enable audit logging"),
    external_domains: z.string().array().optional().describe("Explicitly allowed external domains"),
    allow_external_ips: z.boolean().optional().default(false).describe("Allow external/public IP addresses"),
    allowed_ip_ranges: z.string().array().optional().describe("Additional allowed IP ranges in CIDR notation"),
  })
  .strict()
  .meta({
    ref: "SecurityConfig",
  })

export type SecurityConfigType = z.infer<typeof SecurityConfig>

export function validateUrlFromConfig(url: string, config: SecurityConfigType): ValidationResult {
  if (!url) {
    return { allowed: false, reason: "Empty URL" }
  }

  const host = extractHost(url)

  if (!config) {
    return { allowed: true, reason: "No security configuration" }
  }

  if (isLocalhost(host)) {
    if (config.allow_local) return { allowed: true, reason: "Localhost allowed" }
    return { allowed: false, reason: "Localhost blocked (allow_local is false)" }
  }

  if (isPrivateIP(host)) {
    if (config.allow_private_ip) return { allowed: true, reason: "Private IP allowed" }
    return { allowed: false, reason: "Private/internal IP blocked (allow_private_ip is false)" }
  }

  if (config.allow_internal_dns_suffixes) {
    for (const suffix of config.allow_internal_dns_suffixes) {
      const bare = suffix.replace(/^\./, "")
      if (host === bare || host.endsWith("." + bare)) {
        return { allowed: true, reason: `Internal DNS suffix ${suffix} allowed` }
      }
    }
  }

  if (config.allowed_ip_ranges) {
    for (const cidr of config.allowed_ip_ranges) {
      if (isCIDRMatch(host, cidr)) {
        return { allowed: true, reason: `IP in allowed range: ${cidr}` }
      }
    }
  }

  if (config.external_domains) {
    for (const domain of config.external_domains) {
      if (matchesPattern(host, domain)) {
        return { allowed: true, reason: `Domain in allowlist: ${domain}` }
      }
    }
  }

  if (config.block_domain_regex) {
    for (const regex of config.block_domain_regex) {
      try {
        if (new RegExp(regex).test(host)) {
          return { allowed: false, reason: `Blocked by regex: ${regex}` }
        }
      } catch {
        // Skip invalid regex
      }
    }
  }

  if (config.mode === "strict") {
    return {
      allowed: false,
      reason: `URL not in allowlist (mode: ${config.mode})`,
    }
  }

  if (config.mode === "internal-only") {
    if (config.allow_external_ips && !isPrivateIP(host) && isIP(host)) {
      return {
        allowed: true,
        reason: "External IP allowed (config setting)",
      }
    }
    return {
      allowed: false,
      reason: "External URL blocked (mode: internal-only)",
    }
  }

  return { allowed: true, reason: "External allowed (mode: external-allowed)" }
}

export function online() {
  const nav = globalThis.navigator
  if (!nav || typeof nav.onLine !== "boolean") return true
  return nav.onLine
}

export function proxied() {
  return !!(process.env.HTTP_PROXY || process.env.HTTPS_PROXY || process.env.http_proxy || process.env.https_proxy)
}
