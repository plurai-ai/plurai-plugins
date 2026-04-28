import { describe, expect, it } from "vitest";
import { validateParams } from "../src/validateParams";

const VALID_STATE = "abc12345abc12345"; // 16 chars, base64url
const VALID_REDIRECT = "http://127.0.0.1:8765/callback";

function q(redirectUri: string, state: string): string {
  const enc = (v: string) => encodeURIComponent(v);
  return `?redirect_uri=${enc(redirectUri)}&state=${enc(state)}`;
}

function expectAccepted(
  search: string,
  opts: { hostname?: string; port?: number; state?: string } = {},
): void {
  const r = validateParams(search);
  expect(r.ok).toBe(true);
  if (r.ok) {
    expect(r.redirectUrl.hostname).toBe(opts.hostname ?? "127.0.0.1");
    expect(r.redirectUrl.port).toBe(String(opts.port ?? 8765));
    expect(r.redirectUrl.pathname).toBe("/callback");
    expect(r.redirectUrl.protocol).toBe("http:");
    expect(r.redirectUrl.search).toBe("");
    expect(r.redirectUrl.hash).toBe("");
    expect(r.state).toBe(opts.state ?? VALID_STATE);
  }
}

function expectRejected(search: string): void {
  const r = validateParams(search);
  expect(r.ok).toBe(false);
}

describe("validateParams — accepts", () => {
  it("canonical 127.0.0.1:8765 input", () => {
    expectAccepted(q(VALID_REDIRECT, VALID_STATE));
  });
  it("port 8766", () => {
    expectAccepted(q("http://127.0.0.1:8766/callback", VALID_STATE), { port: 8766 });
  });
  it("port 8767", () => {
    expectAccepted(q("http://127.0.0.1:8767/callback", VALID_STATE), { port: 8767 });
  });
  it("port 8768", () => {
    expectAccepted(q("http://127.0.0.1:8768/callback", VALID_STATE), { port: 8768 });
  });
  it("localhost host (per user decision)", () => {
    expectAccepted(q("http://localhost:8765/callback", VALID_STATE), { hostname: "localhost" });
  });
  it("state at min length (16)", () => {
    const s = "a".repeat(16);
    expectAccepted(q(VALID_REDIRECT, s), { state: s });
  });
  it("state with all base64url chars", () => {
    const r = validateParams(q(VALID_REDIRECT, "ABCdef-_0123456789"));
    expect(r.ok).toBe(true);
  });
});

describe("validateParams — rejects (params shape)", () => {
  it("query string longer than 1024 chars", () => {
    expectRejected("?" + "a".repeat(1025));
  });
  it("missing state", () => {
    expectRejected(`?redirect_uri=${encodeURIComponent(VALID_REDIRECT)}`);
  });
  it("missing redirect_uri", () => {
    expectRejected(`?state=${VALID_STATE}`);
  });
  it("both missing", () => {
    expectRejected("");
  });
  it("duplicate redirect_uri", () => {
    expectRejected(
      `?redirect_uri=${encodeURIComponent(VALID_REDIRECT)}&redirect_uri=${encodeURIComponent(VALID_REDIRECT)}&state=${VALID_STATE}`,
    );
  });
  it("duplicate state", () => {
    expectRejected(
      `?redirect_uri=${encodeURIComponent(VALID_REDIRECT)}&state=${VALID_STATE}&state=${VALID_STATE}`,
    );
  });
  it("unknown extra query key", () => {
    expectRejected(q(VALID_REDIRECT, VALID_STATE) + "&foo=bar");
  });
});

describe("validateParams — rejects (state)", () => {
  it("state shorter than 16 chars", () => {
    expectRejected(q(VALID_REDIRECT, "abc"));
  });
  it("state longer than 256 chars", () => {
    expectRejected(q(VALID_REDIRECT, "a".repeat(257)));
  });
  it("state with `+`", () => {
    expectRejected(q(VALID_REDIRECT, "abc+abcabcabcabc"));
  });
  it("state with `=`", () => {
    expectRejected(q(VALID_REDIRECT, "abcabcabcabcabc="));
  });
  it("state with `/`", () => {
    expectRejected(q(VALID_REDIRECT, "abc/abcabcabcabc"));
  });
  it("state with space", () => {
    expectRejected(q(VALID_REDIRECT, "abc abcabcabcabc"));
  });
  it("state with emoji", () => {
    expectRejected(q(VALID_REDIRECT, "abcabcabcabcabc😀"));
  });
});

describe("validateParams — rejects (redirect_uri scheme/format)", () => {
  it("redirect_uri longer than 512 chars", () => {
    expectRejected(q("http://127.0.0.1:8765/callback?" + "a".repeat(600), VALID_STATE));
  });
  it("https scheme", () => {
    expectRejected(q("https://127.0.0.1:8765/callback", VALID_STATE));
  });
  it("javascript: scheme", () => {
    expectRejected(q("javascript:alert(1)", VALID_STATE));
  });
  it("data: scheme", () => {
    expectRejected(q("data:text/html,x", VALID_STATE));
  });
  it("file: scheme", () => {
    expectRejected(q("file:///etc/passwd", VALID_STATE));
  });
  it("vbscript: scheme", () => {
    expectRejected(q("vbscript:msgbox", VALID_STATE));
  });
  it("chrome-extension: scheme", () => {
    expectRejected(q("chrome-extension://abcdef/callback", VALID_STATE));
  });
  it("malformed URL", () => {
    expectRejected(q(":not-a-url", VALID_STATE));
  });
  it("empty URL", () => {
    expectRejected(q("", VALID_STATE));
  });
  it("userinfo in URL", () => {
    expectRejected(q("http://user:pass@127.0.0.1:8765/callback", VALID_STATE));
  });
  it("only username in URL", () => {
    expectRejected(q("http://user@127.0.0.1:8765/callback", VALID_STATE));
  });
});

describe("validateParams — rejects (host)", () => {
  it("127.0.0.2 (different loopback IP)", () => {
    expectRejected(q("http://127.0.0.2:8765/callback", VALID_STATE));
  });
  it("0.0.0.0 (any-address, not loopback)", () => {
    expectRejected(q("http://0.0.0.0:8765/callback", VALID_STATE));
  });
  it("evil.tld", () => {
    expectRejected(q("http://evil.tld:8765/callback", VALID_STATE));
  });
  it("subdomain confusion 127.0.0.1.evil.tld", () => {
    expectRejected(q("http://127.0.0.1.evil.tld:8765/callback", VALID_STATE));
  });
  it("IPv6 [::1] (not in allowlist)", () => {
    expectRejected(q("http://[::1]:8765/callback", VALID_STATE));
  });
  it("IPv4-mapped IPv6 [::ffff:127.0.0.1]", () => {
    expectRejected(q("http://[::ffff:127.0.0.1]:8765/callback", VALID_STATE));
  });
});

// The WHATWG URL parser canonicalises hex/decimal/octal/trailing-dot IPv4
// representations to their dotted-decimal form *before* we read .hostname.
// For 127.0.0.1 specifically, that means the validator transparently accepts
// these forms. They aren't a security regression: the browser will navigate
// to the same loopback IP regardless of how the input was spelt.
describe("validateParams — accepts URL-normalised loopback forms", () => {
  it("trailing dot 127.0.0.1. → 127.0.0.1", () => {
    expectAccepted(q("http://127.0.0.1.:8765/callback", VALID_STATE));
  });
  it("hex form 0x7f.0.0.1 → 127.0.0.1", () => {
    expectAccepted(q("http://0x7f.0.0.1:8765/callback", VALID_STATE));
  });
  it("decimal form 2130706433 → 127.0.0.1", () => {
    expectAccepted(q("http://2130706433:8765/callback", VALID_STATE));
  });
  it("octal form 0177.0.0.1 → 127.0.0.1", () => {
    expectAccepted(q("http://0177.0.0.1:8765/callback", VALID_STATE));
  });
});

describe("validateParams — rejects (port)", () => {
  it("missing port (defaults to 80)", () => {
    expectRejected(q("http://127.0.0.1/callback", VALID_STATE));
  });
  it("port 0", () => {
    expectRejected(q("http://127.0.0.1:0/callback", VALID_STATE));
  });
  it("port 80", () => {
    expectRejected(q("http://127.0.0.1:80/callback", VALID_STATE));
  });
  it("port 9000 (unallowed)", () => {
    expectRejected(q("http://127.0.0.1:9000/callback", VALID_STATE));
  });
  it("port 65536 (URL parse rejects)", () => {
    expectRejected(q("http://127.0.0.1:65536/callback", VALID_STATE));
  });
});

describe("validateParams — rejects (path)", () => {
  it("trailing slash /callback/", () => {
    expectRejected(q("http://127.0.0.1:8765/callback/", VALID_STATE));
  });
  it("uppercase /CALLBACK", () => {
    expectRejected(q("http://127.0.0.1:8765/CALLBACK", VALID_STATE));
  });
  it("path traversal /callback/../foo", () => {
    expectRejected(q("http://127.0.0.1:8765/callback/../foo", VALID_STATE));
  });
  it("root path /", () => {
    expectRejected(q("http://127.0.0.1:8765/", VALID_STATE));
  });
});

describe("validateParams — rejects (pre-existing query/fragment on redirect_uri)", () => {
  it("pre-existing query string", () => {
    expectRejected(q("http://127.0.0.1:8765/callback?token=fake", VALID_STATE));
  });
  it("pre-existing fragment", () => {
    expectRejected(q("http://127.0.0.1:8765/callback#x", VALID_STATE));
  });
});
