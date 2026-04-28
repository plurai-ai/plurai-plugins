import { describe, expect, it } from "vitest";
import { decodeJwtExp } from "../src/decodeJwt";

function b64url(obj: unknown): string {
  return btoa(JSON.stringify(obj))
    .replace(/=+$/, "")
    .replace(/\+/g, "-")
    .replace(/\//g, "_");
}

function jwt(payload: unknown, opts: { headerSeg?: string; sigSeg?: string } = {}): string {
  const header = opts.headerSeg ?? b64url({ alg: "RS256", typ: "JWT" });
  const sig = opts.sigSeg ?? "sigplaceholder";
  return `${header}.${b64url(payload)}.${sig}`;
}

const future = () => Math.floor(Date.now() / 1000) + 600;
const past = () => Math.floor(Date.now() / 1000) - 600;

describe("decodeJwtExp — accepts", () => {
  it("valid JWT with future numeric exp", () => {
    const exp = future();
    expect(decodeJwtExp(jwt({ exp }))).toBe(exp);
  });
});

describe("decodeJwtExp — rejects", () => {
  it("empty string", () => {
    expect(decodeJwtExp("")).toBeNull();
  });
  it("two segments", () => {
    expect(decodeJwtExp(`${b64url({})}.${b64url({ exp: future() })}`)).toBeNull();
  });
  it("four segments", () => {
    expect(decodeJwtExp(`a.${b64url({ exp: future() })}.b.c`)).toBeNull();
  });
  it("payload has non-base64url char in segment", () => {
    expect(decodeJwtExp(`a.${b64url({ exp: future() })}!.c`)).toBeNull();
  });
  it("payload is not valid base64", () => {
    expect(decodeJwtExp("a.@@@.c")).toBeNull();
  });
  it("payload decodes to non-JSON", () => {
    const garbage = btoa("not json").replace(/=+$/, "");
    expect(decodeJwtExp(`a.${garbage}.c`)).toBeNull();
  });
  it("payload decodes to a non-object (number)", () => {
    expect(decodeJwtExp(jwt(42 as unknown))).toBeNull();
  });
  it("payload decodes to a non-object (string)", () => {
    expect(decodeJwtExp(jwt("hello" as unknown))).toBeNull();
  });
  it("payload decodes to null", () => {
    expect(decodeJwtExp(jwt(null))).toBeNull();
  });
  it("payload missing exp", () => {
    expect(decodeJwtExp(jwt({ sub: "u" }))).toBeNull();
  });
  it("exp is a string, not a number", () => {
    expect(decodeJwtExp(jwt({ exp: "1234567890" }))).toBeNull();
  });
  it("exp is null", () => {
    expect(decodeJwtExp(jwt({ exp: null }))).toBeNull();
  });
  it("exp is NaN", () => {
    expect(decodeJwtExp(jwt({ exp: Number.NaN }))).toBeNull();
  });
  it("exp is Infinity", () => {
    expect(decodeJwtExp(jwt({ exp: Number.POSITIVE_INFINITY }))).toBeNull();
  });
  it("exp is in the past", () => {
    expect(decodeJwtExp(jwt({ exp: past() }))).toBeNull();
  });
  it("exp equals now (treated as expired)", () => {
    const now = Math.floor(Date.now() / 1000);
    expect(decodeJwtExp(jwt({ exp: now }))).toBeNull();
  });
  it("payload with __proto__ key — exp read directly, no pollution", () => {
    const exp = future();
    const tok = jwt({ exp, __proto__: { exp: past() } });
    expect(decodeJwtExp(tok)).toBe(exp);
  });
});
