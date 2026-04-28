// Strict validator for the broker's query string. Constants are hard-coded:
// the allowlist must not be overridable by env vars or other runtime input.
// Tests for every accepted/rejected case live in test/validateParams.test.ts.

const ALLOWED_PORTS = new Set([8765, 8766, 8767, 8768]);
const ALLOWED_HOSTS = new Set(["127.0.0.1", "localhost"]);
const STATE_CHARSET = /^[A-Za-z0-9_-]+$/;
const STATE_MIN = 16;
const STATE_MAX = 256;
const REDIRECT_URI_MAX = 512;
const QUERY_MAX = 1024;
const ALLOWED_KEYS = new Set(["redirect_uri", "state"]);

export type Validated =
  | { ok: true; redirectUrl: URL; state: string }
  | { ok: false; error: string };

const ERR_QUERY_TOO_LONG = "Invalid sign-in URL: query string too long.";
const ERR_UNKNOWN_OR_DUPLICATE_KEY = "Invalid sign-in URL: unexpected query parameter.";
const ERR_MISSING_PARAMS = "Invalid sign-in URL: missing required parameters.";
const ERR_BAD_STATE = "Invalid sign-in URL: state parameter rejected.";
const ERR_BAD_REDIRECT_URI = "Invalid sign-in URL: redirect_uri rejected.";

function devWarn(reason: string, value: string): void {
  if (import.meta.env.DEV) {
    console.warn(`[broker:validateParams] ${reason}:`, value.slice(0, 200));
  }
}

export function validateParams(search: string): Validated {
  if (search.length > QUERY_MAX) {
    devWarn("query too long", String(search.length));
    return { ok: false, error: ERR_QUERY_TOO_LONG };
  }

  const params = new URLSearchParams(search);

  for (const key of params.keys()) {
    if (!ALLOWED_KEYS.has(key)) {
      devWarn("unknown query key", key);
      return { ok: false, error: ERR_UNKNOWN_OR_DUPLICATE_KEY };
    }
  }
  if (params.getAll("redirect_uri").length !== 1 || params.getAll("state").length !== 1) {
    devWarn("missing or duplicate key", search);
    return { ok: false, error: ERR_MISSING_PARAMS };
  }

  const state = params.get("state")!;
  if (state.length < STATE_MIN || state.length > STATE_MAX || !STATE_CHARSET.test(state)) {
    devWarn("bad state", state);
    return { ok: false, error: ERR_BAD_STATE };
  }

  const redirectUri = params.get("redirect_uri")!;
  if (redirectUri.length > REDIRECT_URI_MAX) {
    devWarn("redirect_uri too long", String(redirectUri.length));
    return { ok: false, error: ERR_BAD_REDIRECT_URI };
  }

  let url: URL;
  try {
    url = new URL(redirectUri);
  } catch {
    devWarn("redirect_uri not parseable", redirectUri);
    return { ok: false, error: ERR_BAD_REDIRECT_URI };
  }

  if (url.protocol !== "http:") return rejectRedirect("scheme", url.protocol);
  if (url.username !== "" || url.password !== "") return rejectRedirect("userinfo", url.href);
  if (!ALLOWED_HOSTS.has(url.hostname)) return rejectRedirect("hostname", url.hostname);
  if (!/^\d{1,5}$/.test(url.port)) return rejectRedirect("port format", url.port);
  const port = Number(url.port);
  if (!ALLOWED_PORTS.has(port)) return rejectRedirect("port not allowed", url.port);
  if (url.pathname !== "/callback") return rejectRedirect("pathname", url.pathname);
  if (url.search !== "") return rejectRedirect("pre-existing query", url.search);
  if (url.hash !== "") return rejectRedirect("pre-existing fragment", url.hash);

  // Build a fresh URL from validated components — discards any non-canonical
  // input the URL parser might have preserved (e.g. authority quirks).
  return {
    ok: true,
    redirectUrl: new URL(`http://${url.hostname}:${port}/callback`),
    state,
  };
}

function rejectRedirect(reason: string, value: string): Validated {
  devWarn(`redirect_uri ${reason}`, value);
  return { ok: false, error: ERR_BAD_REDIRECT_URI };
}
