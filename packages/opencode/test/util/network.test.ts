import { describe, it, expect } from "bun:test"
import {
  validateUrlFromConfig,
  isPrivateIP,
  isLocalhost,
  isCIDRMatch,
  isInternalDNS,
  extractHost,
  normalizeIPv4MappedIPv6,
} from "@/util/network"

describe("network validation", () => {
  describe("isLocalhost", () => {
    it("returns true for localhost", () => {
      expect(isLocalhost("localhost")).toBe(true)
    })

    it("returns true for 127.0.0.1", () => {
      expect(isLocalhost("127.0.0.1")).toBe(true)
    })

    it("returns true for ::1", () => {
      expect(isLocalhost("::1")).toBe(true)
    })

    it("returns false for example.com", () => {
      expect(isLocalhost("example.com")).toBe(false)
    })
  })

  describe("isPrivateIP", () => {
    it("returns true for 10.0.0.1 (Class A)", () => {
      expect(isPrivateIP("10.0.0.1")).toBe(true)
    })

    it("returns true for 10.255.255.255", () => {
      expect(isPrivateIP("10.255.255.255")).toBe(true)
    })

    it("returns true for 172.16.0.1 (Class B)", () => {
      expect(isPrivateIP("172.16.0.1")).toBe(true)
    })

    it("returns true for 172.31.255.255", () => {
      expect(isPrivateIP("172.31.255.255")).toBe(true)
    })

    it("returns false for 172.15.0.1 (outside Class B range)", () => {
      expect(isPrivateIP("172.15.0.1")).toBe(false)
    })

    it("returns false for 172.32.0.1 (outside Class B range)", () => {
      expect(isPrivateIP("172.32.0.1")).toBe(false)
    })

    it("returns true for 192.168.0.1 (Class C)", () => {
      expect(isPrivateIP("192.168.0.1")).toBe(true)
    })

    it("returns true for 192.168.255.255", () => {
      expect(isPrivateIP("192.168.255.255")).toBe(true)
    })

    it("returns true for 127.0.0.1 (Loopback)", () => {
      expect(isPrivateIP("127.0.0.1")).toBe(true)
    })

    it("returns false for 8.8.8.8 (Public IP)", () => {
      expect(isPrivateIP("8.8.8.8")).toBe(false)
    })

    it("returns false for google.com (not an IP)", () => {
      expect(isPrivateIP("google.com")).toBe(false)
    })

    it("returns true for ::1 (IPv6 loopback)", () => {
      expect(isPrivateIP("::1")).toBe(true)
    })
  })

  describe("isCIDRMatch", () => {
    it("matches IPv4 in /8 range", () => {
      expect(isCIDRMatch("10.0.0.5", "10.0.0.0/8")).toBe(true)
    })

    it("does not match IPv4 outside /24 range", () => {
      expect(isCIDRMatch("10.0.1.5", "10.0.0.0/24")).toBe(false)
    })

    it("matches IPv4 in /24 range", () => {
      expect(isCIDRMatch("192.168.1.100", "192.168.1.0/24")).toBe(true)
    })

    it("handles 0.0.0.0/0 (all IPs)", () => {
      expect(isCIDRMatch("8.8.8.8", "0.0.0.0/0")).toBe(true)
    })

    describe("IPv6 compressed addresses", () => {
      it("matches compressed IPv6 with ::1", () => {
        expect(isCIDRMatch("::1", "::1/128")).toBe(true)
      })

      it("matches compressed IPv6 with ::/0", () => {
        expect(isCIDRMatch("2001:db8::1", "::/0")).toBe(true)
      })

      it("matches IPv6 /32 prefix", () => {
        expect(isCIDRMatch("2001:db8::1", "2001:db8::/32")).toBe(true)
      })

      it("matches IPv6 /64 prefix", () => {
        expect(isCIDRMatch("2001:db8:1234::1", "2001:db8:1234::/64")).toBe(true)
      })

      it("does not match IPv6 outside /64 range", () => {
        expect(isCIDRMatch("2001:db8:1235::1", "2001:db8:1234::/64")).toBe(false)
      })

      it("matches full compressed IPv6", () => {
        expect(isCIDRMatch("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::/32")).toBe(true)
      })

      it("matches IPv6 /64 with full address", () => {
        expect(isCIDRMatch("2001:0db8:1234:0000:0000:0000:0000:0001", "2001:db8:1234::/64")).toBe(true)
      })

      it("matches IPv6 /128 exact address", () => {
        expect(isCIDRMatch("2001:db8::1", "2001:db8::1/128")).toBe(true)
      })

      it("does not match IPv6 /128 different address", () => {
        expect(isCIDRMatch("2001:db8::2", "2001:db8::1/128")).toBe(false)
      })

      it("matches IPv6 all zeros with ::/0", () => {
        expect(isCIDRMatch("::", "::/0")).toBe(true)
      })

      it("matches IPv6 all ones with ::/0", () => {
        expect(isCIDRMatch("ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "::/0")).toBe(true)
      })

      it("matches IPv6 partial compression", () => {
        expect(isCIDRMatch("2001:db8::1:1", "2001:db8::/32")).toBe(true)
      })

      it("handles IPv6 /48 prefix", () => {
        expect(isCIDRMatch("2001:db8:0000::1", "2001:db8::/48")).toBe(true)
      })

      it("does not match IPv6 /48 outside range", () => {
        expect(isCIDRMatch("2001:db9::1", "2001:db8::/48")).toBe(false)
      })
    })
  })

  describe("validateUrlFromConfig", () => {
    const strictConfig = {
      mode: "strict" as const,
      allow_internal_dns_suffixes: [".internal.corp.com"],
      external_domains: ["api.ok.com"],
      allow_local: true,
      allow_private_ip: true,
      models_dev_enabled: true,
      audit_log_enabled: true,
      allow_external_ips: false,
    }

    it("allows localhost in strict mode", () => {
      const result = validateUrlFromConfig("http://localhost:3000", strictConfig)
      expect(result.allowed).toBe(true)
    })

    it("allows private IPs in strict mode", () => {
      const result = validateUrlFromConfig("http://10.0.0.1:8080", strictConfig)
      expect(result.allowed).toBe(true)
    })

    it("blocks external URLs in strict mode", () => {
      const result = validateUrlFromConfig("https://api.openai.com", strictConfig)
      expect(result.allowed).toBe(false)
    })

    it("allows external domains in allowlist", () => {
      const result = validateUrlFromConfig("https://api.ok.com", strictConfig)
      expect(result.allowed).toBe(true)
    })

    it("allows internal DNS suffixes", () => {
      const result = validateUrlFromConfig("http://api.internal.corp.com", strictConfig)
      expect(result.allowed).toBe(true)
    })

    it("rejects non-matching internal suffixes", () => {
      const result = validateUrlFromConfig("http://api.example.internal", strictConfig)
      expect(result.allowed).toBe(false)
    })

    it("returns all-allowed when no config", () => {
      const result = validateUrlFromConfig("https://api.openai.com", undefined as any)
      expect(result.allowed).toBe(true)
    })

    it("allows all in external-allowed mode", () => {
      const result = validateUrlFromConfig("https://api.openai.com", {
        mode: "external-allowed",
        allow_local: true,
        allow_private_ip: true,
        models_dev_enabled: true,
        audit_log_enabled: true,
        allow_external_ips: false,
      })
      expect(result.allowed).toBe(true)
    })

    it("blocks in strict mode when not in allowlist", () => {
      const result = validateUrlFromConfig("https://api.evil.com", {
        mode: "strict",
        external_domains: ["api.ok.com"],
        allow_local: true,
        allow_private_ip: true,
        models_dev_enabled: true,
        audit_log_enabled: true,
        allow_external_ips: false,
      })
      expect(result.allowed).toBe(false)
    })

    describe("allow_external_ips", () => {
      it("allows external IP in internal-only mode when allow_external_ips is true", () => {
        const result = validateUrlFromConfig("https://8.8.8.8:443", {
          mode: "internal-only",
          allow_local: true,
          allow_private_ip: true,
          allow_external_ips: true,
          models_dev_enabled: true,
          audit_log_enabled: true,
        })
        expect(result.allowed).toBe(true)
      })

      it("blocks DNS name in internal-only mode even when allow_external_ips is true", () => {
        const result = validateUrlFromConfig("https://example.com:443", {
          mode: "internal-only",
          allow_local: true,
          allow_private_ip: true,
          allow_external_ips: true,
          models_dev_enabled: true,
          audit_log_enabled: true,
        })
        expect(result.allowed).toBe(false)
      })

      it("blocks external IP when allow_external_ips is false", () => {
        const result = validateUrlFromConfig("https://8.8.8.8:443", {
          mode: "internal-only",
          allow_local: true,
          allow_private_ip: true,
          allow_external_ips: false,
          models_dev_enabled: true,
          audit_log_enabled: true,
        })
        expect(result.allowed).toBe(false)
      })

      it("allows IPv6 external address when allow_external_ips is true", () => {
        const result = validateUrlFromConfig("https://[2001:db8::1]:443", {
          mode: "internal-only",
          allow_local: true,
          allow_private_ip: true,
          allow_external_ips: true,
          models_dev_enabled: true,
          audit_log_enabled: true,
        })
        expect(result.allowed).toBe(true)
      })

      it("blocks localhost even with allow_external_ips (not an external IP)", () => {
        const result = validateUrlFromConfig("http://localhost:3000", {
          mode: "internal-only",
          allow_local: true,
          allow_private_ip: true,
          allow_external_ips: true,
          models_dev_enabled: true,
          audit_log_enabled: true,
        })
        expect(result.allowed).toBe(true)
      })

      it("blocks private IP even with allow_external_ips (not an external IP)", () => {
        const result = validateUrlFromConfig("http://10.0.0.1:8080", {
          mode: "internal-only",
          allow_local: true,
          allow_private_ip: true,
          allow_external_ips: true,
          models_dev_enabled: true,
          audit_log_enabled: true,
        })
        expect(result.allowed).toBe(true)
      })
    })
  })

  // Security regression tests for identified vulnerabilities

  describe("normalizeIPv4MappedIPv6", () => {
    it("normalizes ::ffff:7f00:1 to 127.0.0.1", () => {
      expect(normalizeIPv4MappedIPv6("::ffff:7f00:1")).toBe("127.0.0.1")
    })

    it("normalizes ::ffff:a9fe:a9fe to 169.254.169.254", () => {
      expect(normalizeIPv4MappedIPv6("::ffff:a9fe:a9fe")).toBe("169.254.169.254")
    })

    it("normalizes dotted form ::ffff:127.0.0.1 to 127.0.0.1", () => {
      expect(normalizeIPv4MappedIPv6("::ffff:127.0.0.1")).toBe("127.0.0.1")
    })

    it("normalizes ::ffff:a00:5 to 10.0.0.5", () => {
      expect(normalizeIPv4MappedIPv6("::ffff:a00:5")).toBe("10.0.0.5")
    })

    it("normalizes ::ffff:c0a8:101 to 192.168.1.1", () => {
      expect(normalizeIPv4MappedIPv6("::ffff:c0a8:101")).toBe("192.168.1.1")
    })

    it("is case-insensitive", () => {
      expect(normalizeIPv4MappedIPv6("::FFFF:7F00:1")).toBe("127.0.0.1")
    })

    it("passes through regular IPv4 addresses", () => {
      expect(normalizeIPv4MappedIPv6("127.0.0.1")).toBe("127.0.0.1")
    })

    it("passes through regular IPv6 addresses", () => {
      expect(normalizeIPv4MappedIPv6("2001:db8::1")).toBe("2001:db8::1")
    })

    it("passes through hostnames", () => {
      expect(normalizeIPv4MappedIPv6("example.com")).toBe("example.com")
    })
  })

  describe("extractHost - IPv4-mapped IPv6 normalization", () => {
    it("normalizes IPv4-mapped IPv6 URL to IPv4", () => {
      const host = extractHost("http://[::ffff:127.0.0.1]:8080/path")
      expect(host).toBe("127.0.0.1")
    })

    it("normalizes hex-form IPv4-mapped IPv6 URL to IPv4", () => {
      // URL parser normalizes ::ffff:127.0.0.1 to ::ffff:7f00:1
      const host = extractHost("http://[::ffff:7f00:1]:8080/")
      expect(host).toBe("127.0.0.1")
    })
  })

  describe("isLocalhost - IPv4-mapped IPv6 bypass prevention", () => {
    it("detects IPv4-mapped IPv6 loopback ::ffff:7f00:1", () => {
      expect(isLocalhost("::ffff:7f00:1")).toBe(true)
    })

    it("detects IPv4-mapped IPv6 loopback dotted form", () => {
      expect(isLocalhost("::ffff:127.0.0.1")).toBe(true)
    })

    it("detects 0.0.0.0 as localhost", () => {
      expect(isLocalhost("0.0.0.0")).toBe(true)
    })
  })

  describe("isPrivateIP - link-local and cloud metadata (169.254.0.0/16)", () => {
    it("returns true for 169.254.169.254 (cloud metadata endpoint)", () => {
      expect(isPrivateIP("169.254.169.254")).toBe(true)
    })

    it("returns true for 169.254.0.1 (link-local start)", () => {
      expect(isPrivateIP("169.254.0.1")).toBe(true)
    })

    it("returns true for 169.254.255.255 (link-local end)", () => {
      expect(isPrivateIP("169.254.255.255")).toBe(true)
    })

    it("returns false for 169.253.0.1 (not link-local)", () => {
      expect(isPrivateIP("169.253.0.1")).toBe(false)
    })

    it("returns false for 169.255.0.1 (not link-local)", () => {
      expect(isPrivateIP("169.255.0.1")).toBe(false)
    })
  })

  describe("isPrivateIP - carrier-grade NAT (100.64.0.0/10)", () => {
    it("returns true for 100.64.0.1", () => {
      expect(isPrivateIP("100.64.0.1")).toBe(true)
    })

    it("returns true for 100.127.255.255", () => {
      expect(isPrivateIP("100.127.255.255")).toBe(true)
    })

    it("returns false for 100.63.255.255 (below range)", () => {
      expect(isPrivateIP("100.63.255.255")).toBe(false)
    })

    it("returns false for 100.128.0.1 (above range)", () => {
      expect(isPrivateIP("100.128.0.1")).toBe(false)
    })
  })

  describe("isPrivateIP - 0.0.0.0/8", () => {
    it("returns true for 0.0.0.0", () => {
      expect(isPrivateIP("0.0.0.0")).toBe(true)
    })

    it("returns true for 0.0.0.1", () => {
      expect(isPrivateIP("0.0.0.1")).toBe(true)
    })
  })

  describe("isPrivateIP - IPv4-mapped IPv6 bypass prevention", () => {
    it("detects ::ffff:7f00:1 as private (maps to 127.0.0.1)", () => {
      expect(isPrivateIP("::ffff:7f00:1")).toBe(true)
    })

    it("detects ::ffff:a00:5 as private (maps to 10.0.0.5)", () => {
      expect(isPrivateIP("::ffff:a00:5")).toBe(true)
    })

    it("detects ::ffff:a9fe:a9fe as private (maps to 169.254.169.254)", () => {
      expect(isPrivateIP("::ffff:a9fe:a9fe")).toBe(true)
    })

    it("detects ::ffff:c0a8:101 as private (maps to 192.168.1.1)", () => {
      expect(isPrivateIP("::ffff:c0a8:101")).toBe(true)
    })

    it("does not flag ::ffff:808:808 as private (maps to 8.8.8.8)", () => {
      expect(isPrivateIP("::ffff:808:808")).toBe(false)
    })
  })

  describe("isPrivateIP - IPv6 private ranges", () => {
    it("detects fe80::1 as private (link-local)", () => {
      expect(isPrivateIP("fe80::1")).toBe(true)
    })

    it("detects fd00::1 as private (unique local)", () => {
      expect(isPrivateIP("fd00::1")).toBe(true)
    })

    it("detects fc00::1 as private (unique local)", () => {
      expect(isPrivateIP("fc00::1")).toBe(true)
    })

    it("does not flag 2001:db8::1 as private (documentation range, but public)", () => {
      expect(isPrivateIP("2001:db8::1")).toBe(false)
    })
  })

  describe("isInternalDNS - domain boundary enforcement", () => {
    it("matches exact domain without leading dot", () => {
      expect(isInternalDNS("corp.com", ["corp.com"])).toBe(true)
    })

    it("matches subdomain with leading dot suffix", () => {
      expect(isInternalDNS("api.corp.com", [".corp.com"])).toBe(true)
    })

    it("matches subdomain without leading dot suffix", () => {
      expect(isInternalDNS("api.corp.com", ["corp.com"])).toBe(true)
    })

    it("rejects evil-corp.com when suffix is corp.com (domain boundary)", () => {
      expect(isInternalDNS("evil-corp.com", ["corp.com"])).toBe(false)
    })

    it("rejects attackercorp.com when suffix is corp.com", () => {
      expect(isInternalDNS("attackercorp.com", ["corp.com"])).toBe(false)
    })

    it("rejects notcorp.com when suffix is corp.com", () => {
      expect(isInternalDNS("notcorp.com", ["corp.com"])).toBe(false)
    })

    it("rejects evil-corp.com when suffix is .corp.com", () => {
      expect(isInternalDNS("evil-corp.com", [".corp.com"])).toBe(false)
    })
  })

  describe("validateUrlFromConfig - cloud metadata endpoint", () => {
    const restrictedConfig = {
      mode: "external-allowed" as const,
      allow_local: false,
      allow_private_ip: false,
      models_dev_enabled: true,
      audit_log_enabled: true,
      allow_external_ips: false,
    }

    it("blocks 169.254.169.254 when private IPs are disallowed", () => {
      const result = validateUrlFromConfig("http://169.254.169.254/latest/meta-data/", restrictedConfig)
      expect(result.allowed).toBe(false)
    })

    it("blocks IPv4-mapped IPv6 loopback when local is disallowed", () => {
      const result = validateUrlFromConfig("http://[::ffff:127.0.0.1]:8080/", restrictedConfig)
      expect(result.allowed).toBe(false)
    })

    it("blocks IPv4-mapped IPv6 private IP when private IPs are disallowed", () => {
      const result = validateUrlFromConfig("http://[::ffff:10.0.0.5]:8080/", restrictedConfig)
      expect(result.allowed).toBe(false)
    })

    it("allows 169.254.169.254 when private IPs are allowed", () => {
      const result = validateUrlFromConfig("http://169.254.169.254/latest/meta-data/", {
        ...restrictedConfig,
        allow_private_ip: true,
      })
      expect(result.allowed).toBe(true)
    })
  })

  describe("validateUrlFromConfig - DNS suffix boundary enforcement", () => {
    const configWithSuffix = {
      mode: "strict" as const,
      allow_internal_dns_suffixes: ["corp.com"],
      allow_local: false,
      allow_private_ip: false,
      models_dev_enabled: true,
      audit_log_enabled: true,
      allow_external_ips: false,
    }

    it("allows exact match corp.com", () => {
      const result = validateUrlFromConfig("https://corp.com", configWithSuffix)
      expect(result.allowed).toBe(true)
    })

    it("allows subdomain api.corp.com", () => {
      const result = validateUrlFromConfig("https://api.corp.com", configWithSuffix)
      expect(result.allowed).toBe(true)
    })

    it("blocks evil-corp.com (no domain boundary)", () => {
      const result = validateUrlFromConfig("https://evil-corp.com", configWithSuffix)
      expect(result.allowed).toBe(false)
    })

    it("blocks attackercorp.com (no domain boundary)", () => {
      const result = validateUrlFromConfig("https://attackercorp.com", configWithSuffix)
      expect(result.allowed).toBe(false)
    })
  })

  describe("validateUrlFromConfig - IPv4-mapped IPv6 in internal-only mode", () => {
    it("blocks IPv4-mapped IPv6 private address when allow_external_ips is true", () => {
      const result = validateUrlFromConfig("http://[::ffff:10.0.0.5]:8080/", {
        mode: "internal-only",
        allow_local: false,
        allow_private_ip: false,
        allow_external_ips: true,
        models_dev_enabled: true,
        audit_log_enabled: true,
      })
      expect(result.allowed).toBe(false)
    })

    it("blocks IPv4-mapped IPv6 metadata address when allow_external_ips is true", () => {
      const result = validateUrlFromConfig("http://[::ffff:169.254.169.254]/", {
        mode: "internal-only",
        allow_local: false,
        allow_private_ip: false,
        allow_external_ips: true,
        models_dev_enabled: true,
        audit_log_enabled: true,
      })
      expect(result.allowed).toBe(false)
    })
  })
})
