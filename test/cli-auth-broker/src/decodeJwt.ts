// Strict, defensive JWT exp parser. Returns null for anything that isn't a
// well-formed JWT with a numeric, future-dated `exp` claim. Caller must
// refuse to forward the token if this returns null — without a parseable
// expiry the CLI would persist the JWT with expires_at=0 and re-pop the
// browser flow on every subsequent request.

const SEGMENT_CHARSET = /^[A-Za-z0-9_-]+={0,2}$/;

export function decodeJwtExp(jwt: string): number | null {
  const parts = jwt.split(".");
  if (parts.length !== 3) return null;
  const payloadB64 = parts[1];
  if (!SEGMENT_CHARSET.test(payloadB64)) return null;

  let payload: unknown;
  try {
    const std = payloadB64.replace(/-/g, "+").replace(/_/g, "/");
    payload = JSON.parse(atob(std));
  } catch {
    return null;
  }
  if (!payload || typeof payload !== "object") return null;

  const exp = (payload as Record<string, unknown>).exp;
  if (typeof exp !== "number" || !Number.isFinite(exp)) return null;
  if (exp <= Math.floor(Date.now() / 1000)) return null;
  return exp;
}
