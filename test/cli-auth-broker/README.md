# cli-auth-broker

Production broker page for Pluto CLI authentication, per
[`../../docs/rfcs/0001-web-broker-cli-auth.md`](../../docs/rfcs/0001-web-broker-cli-auth.md).

A Vite + React single-page app whose only job is to:

1. Read `redirect_uri` and `state` from the query string and validate strictly
   ([`src/validateParams.ts`](src/validateParams.ts)).
2. Use `@clerk/clerk-react` to ensure the user is signed in (redirects to
   Clerk's hosted sign-in if not).
3. Call `clerk.session.getToken({ template: 'pluto-agent-authz' })` to mint a
   JWT with `aud: svc:pluto-agent`.
4. Redirect the browser to
   `${redirect_uri}?state=<state>&token=<jwt>&expires_at=<epoch>`.

Because every input is attacker-controllable and the output is a bearer
token, every error path is a credential-leak path. This page is hardened
accordingly — see the [Security model](#security-model) section.

## Setup

```bash
cd test/cli-auth-broker
cp .env.example .env.local
# edit .env.local — paste the Clerk publishable key for the instance you want
# (Clerk dashboard → API keys → "Publishable key", starts with pk_test_ / pk_live_)
npm ci
```

The Clerk dashboard's **Authorized origins** list must include the origin
you serve this app from. For local dev: `http://127.0.0.1:5173`. For
staging: `https://pluto.stg.plurai.ai`. Without it, `getToken()` silently
fails the CORS preflight and the page emits a misleading `mint_failed`.

## Run the broker page

```bash
npm run dev
# → http://127.0.0.1:5173/cli-auth?redirect_uri=...&state=...
```

Vite serves `index.html` for any path (SPA fallback), so the app handles
`/cli-auth?...` URLs even though there's only one route.

## Run the end-to-end flow test

In a second terminal:

```bash
npm run test:flow
```

This script ([`test-flow.js`](test-flow.js)) stands in for the CLI: starts
a loopback server on `127.0.0.1:8765/callback`, opens the broker page in
your default browser with a random `state`, and prints the captured JWT +
decoded payload when the redirect arrives.

## Run the unit tests

```bash
npm run test          # one-shot
npm run test:watch    # watch mode
```

Tests cover `validateParams` (~50 accepted/rejected cases including
hostname-canonicalisation tricks, port edge cases, scheme injection,
pre-existing query/fragment, duplicate keys, oversize input) and
`decodeJwtExp` (segment count, charset, JSON validity, exp shape,
expiry, prototype-pollution-resistant key access).

## Build

```bash
npm run build
```

Production build settings ([`vite.config.ts`](vite.config.ts)):

- `sourcemap: false` — never ship maps to prod.
- `esbuild.drop: ['console', 'debugger']` — belt-and-braces against any
  accidental `console.*` reaching prod.
- Hashed asset filenames for cache-busting + immutable headers.

Bundle hygiene checks (suitable for CI):

```bash
npm run build
grep -r 'sk_'      dist/   # must be empty (no Clerk secret keys)
grep -r 'console\.' dist/  # must be empty (drop confirmed)
ls dist/assets/*.map        # must be empty (no source maps)
```

## Configuration

The Clerk publishable key is the only env-driven config. It is read at build
time:

| Variable | Required | Notes |
|----------|----------|-------|
| `VITE_CLERK_PUBLISHABLE_KEY` | yes | From Clerk dashboard → API keys. |

Everything else (JWT template name, redirect-host allowlist, redirect-port
allowlist) is **hard-coded** in [`src/validateParams.ts`](src/validateParams.ts)
and [`src/App.tsx`](src/App.tsx). It is intentionally not env-overridable in
production.

## Security model

The full threat model and hardening rationale is in the project plan
(`/Users/ben/.claude/plans/check-docs-rfcs-0001-web-broker-cli-auth-serene-quill.md`).
The short version:

### What this page defends against

- **Open redirect / token exfil.** Strict validator: hardcoded host
  allowlist (`127.0.0.1`, `localhost`), port allowlist (`8765–8768`), exact
  `/callback` path, no userinfo, no pre-existing query/fragment, no
  duplicate keys, length caps, `state` charset (`[A-Za-z0-9_-]{16,256}`).
  See [`src/validateParams.ts`](src/validateParams.ts).
- **Reflected attacker text in error UI.** Errors are fixed strings; raw
  attacker input only goes to `console.warn` in dev.
- **Token leakage into telemetry / DOM / state.** Token lives in `useRef`,
  cleared immediately after redirect. Never `useState`'d, rendered, or
  logged. Global `error` / `unhandledrejection` listeners suppress bubbling
  ([`src/main.tsx`](src/main.tsx)).
- **bfcache replay on the back button.** `sessionStorage` completion flag,
  scoped to `state`. Belt: server-side `Cache-Control: no-store`.
- **React StrictMode double-mint.** `useRef` started-flag.
- **Malformed / already-expired JWT forwarded to CLI.** Strict
  [`src/decodeJwt.ts`](src/decodeJwt.ts); refuse to send if exp can't be
  parsed or is already in the past.
- **Clickjacking / framing.** JS frame-buster in
  [`src/main.tsx`](src/main.tsx). Real defence is server-side
  `frame-ancestors 'none'` + `X-Frame-Options: DENY` (see deploy-headers
  section below).
- **HTTPS downgrade.** Production build refuses to run on `http:`.
- **Stale Clerk session at `getToken` time.** `clerk.session` re-checked
  before the call; closed-enum error redirect if missing.
- **CLI hang on broker failure.** All post-validation failures redirect
  with `?error=<code>&error_description=<short>`, where `<code>` ∈
  `{mint_failed, session_expired}`.

### Required server-side headers (apply when hosting at /cli-auth)

```
Content-Security-Policy:
  default-src 'none';
  script-src 'self' https://*.clerk.accounts.dev https://*.clerk.com https://*.clerk.dev;
  connect-src 'self' https://*.clerk.accounts.dev https://*.clerk.com https://*.clerk.dev https://clerk-telemetry.com;
  img-src 'self' data: https://img.clerk.com;
  style-src 'self' 'unsafe-inline';
  font-src 'self' data:;
  form-action 'none';
  frame-ancestors 'none';
  base-uri 'none';
  object-src 'none';
  upgrade-insecure-requests
Strict-Transport-Security: max-age=63072000; includeSubDomains; preload
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Permissions-Policy: accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=(), interest-cohort=(), browsing-topics=()
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
Origin-Agent-Cluster: ?1
Cache-Control: no-store, no-cache, must-revalidate, private
Pragma: no-cache
Expires: 0
```

For long-cached static assets at `/assets/*`, use a separate rule:
`Cache-Control: public, max-age=31536000, immutable`. Do not apply that
to the HTML.

Deploy CSP in `Content-Security-Policy-Report-Only` for ~1 week and collect
violation reports before flipping to enforce. Confirm exact Clerk hostnames
against the staging Network panel.

### Residual risks (not fixable from this page)

- **JWT in browser history of the loopback URL.** The CLI's success-page
  HTML should run `history.replaceState(null, '', '/callback')` immediately
  on load. Mitigated by the 10-min token lifetime.
- **JWT in CLI loopback access log.** The CLI's `BaseHTTPRequestHandler`
  must override `log_message` to no-op, or strip the query string.
- **Browser extensions** with host permissions on `pluto.plurai.ai/*` can
  read `document.URL`. Use a clean profile when running `auth login` if
  high-privilege extensions are installed.
- **Compromised Clerk JS / CDN.** Subscribe to Clerk security advisories.
- **Local malicious process binding `127.0.0.1:8765` before the CLI does.**
  CLI side defends with `state`; the legitimate side's loopback rejects
  on state mismatch.

### Coordination items for CLI side (`src/auth.py`)

These are not broker-page changes but are required for the hardening to be
end-to-end complete:

1. Recognise `?error=<code>` on `/callback` and surface a friendly message.
2. Override `BaseHTTPRequestHandler.log_message` so `?token=` doesn't go to
   stderr.
3. Success-page HTML scrubs the URL bar via `history.replaceState`.
4. Validate `Sec-Fetch-Mode: navigate` + `Sec-Fetch-Dest: document` on
   `/callback` to defang any local cross-page `fetch()` exfil.
5. Bind only `127.0.0.1`, never `localhost` / `0.0.0.0` / `::`.

## Porting the page into the prod Pluto webapp

Carries over verbatim:

- [`src/validateParams.ts`](src/validateParams.ts) — pure function.
- [`src/decodeJwt.ts`](src/decodeJwt.ts) — pure function.
- The orchestration logic in [`src/App.tsx`](src/App.tsx).
- The frame-buster + HTTPS + error-suppression guards from
  [`src/main.tsx`](src/main.tsx).

Must be re-asserted at the new layer:

- **Per-route headers** (above). Site-wide CSP must not loosen `/cli-auth`.
- **Server-side `validateParams` mirror** if the route is server-rendered.
- **Service worker bypass** for `/cli-auth` if the parent webapp registers
  one.
- **Disable RUM / analytics / session replay on `/cli-auth`.** Sentry,
  Datadog, FullStory, LogRocket, Mixpanel, GA4, etc. — each captures URLs;
  the broker URL contains the token between mint and redirect.
- **No layout chrome** — don't render the webapp's nav/sidebar/footer on
  this route.
- **Clerk Authorized origins** — add the prod and staging webapp origins
  exactly.

## Files

```
test/cli-auth-broker/
├── package.json
├── tsconfig.json
├── vite.config.ts
├── vitest.config.ts
├── index.html              # CSP meta, robots, referrer
├── src/
│   ├── main.tsx            # Frame-buster, HTTPS guard, error boundary, ClerkProvider
│   ├── App.tsx             # Phase state machine, ref-based token, error redirect
│   ├── validateParams.ts   # Strict redirect_uri/state validator
│   ├── decodeJwt.ts        # Defensive JWT exp parser
│   └── vite-env.d.ts
├── test/
│   ├── validateParams.test.ts
│   └── decodeJwt.test.ts
├── test-flow.js            # Loopback receiver, simulates the CLI side
├── .env.example
├── .gitignore
└── README.md
```
