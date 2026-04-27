# cli-auth-broker

Reference implementation of the broker page described in
[`../../docs/rfcs/0001-web-broker-cli-auth.md`](../../docs/rfcs/0001-web-broker-cli-auth.md).

A Vite + React single-page app that:

1. Reads `redirect_uri` and `state` from the query string and validates the redirect URI (loopback host + allowed port + `/callback` path).
2. Uses `@clerk/clerk-react` to ensure the user is signed in (redirects to Clerk's hosted sign-in if not).
3. Calls `clerk.session.getToken({ template: 'pluto-agent-authz' })` to mint a JWT with `aud: svc:pluto-agent`.
4. Redirects the browser to `${redirect_uri}?state=<state>&token=<jwt>&expires_at=<epoch>`.

The page exists for two purposes:

- **Local end-to-end testing** of the CLI side without waiting for backend integration.
- **Reference port** — the Pluto webapp team can lift `src/App.tsx` (and the `validateParams` helper inside it) into the production webapp's routing, with at most cosmetic changes.

## Setup

```bash
cd test/cli-auth-broker
cp .env.example .env.local
# edit .env.local — paste the Clerk publishable key for the instance you want
# (Clerk dashboard → API keys → "Publishable key", starts with pk_test_ / pk_live_)
npm install
```

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

This script (`test-flow.js`) stands in for the CLI: starts a loopback server on
`127.0.0.1:8765/callback`, opens the broker page in your default browser with a
random `state`, and prints the captured JWT + decoded payload when the redirect
arrives. Compare the payload to what the production Pluto API expects (see
the JWT diff in the RFC).

## Configuration (env vars)

All read from `.env.local` at build / dev-server start.

| Variable | Default | Notes |
|----------|---------|-------|
| `VITE_CLERK_PUBLISHABLE_KEY` | _required_ | From Clerk dashboard → API keys. |
| `VITE_JWT_TEMPLATE` | `pluto-agent-authz` | Must match a JWT template on the Clerk instance. |
| `VITE_ALLOWED_REDIRECT_HOSTS` | `127.0.0.1,localhost` | Comma-separated allowlist. |
| `VITE_ALLOWED_REDIRECT_PORTS` | `8765,8766,8767,8768` | Must include whatever ports the CLI binds (`REGISTERED_PORTS` in `src/auth.py`). |

## Backend integration notes

For Pluto's webapp to host this in production:

- Mount `App.tsx` at `/cli-auth` in the existing routing (Next.js: `app/cli-auth/page.tsx`; React Router: a new route).
- The webapp's existing `<ClerkProvider>` is reused — drop the one in `src/main.tsx`.
- Move config out of Vite env vars into whatever the webapp uses for runtime config (Next.js `process.env.NEXT_PUBLIC_*`, etc.).
- The `validateParams` function and the `useEffect` that mints + redirects are framework-agnostic — copy them as-is.
- The redirect-URI allowlist (host + port) MUST be enforced server-side too if the route ever gets backend-side rendering. Client-only validation is acceptable for the test broker because the only thing the page sends to the redirect target is a token derived from the user's own Clerk session — the user can't trick themselves into leaking it to a different host that they don't control.

## Files

```
test/cli-auth-broker/
├── package.json
├── tsconfig.json
├── vite.config.ts
├── index.html              # Vite entry
├── src/
│   ├── main.tsx            # Mounts <ClerkProvider><App /></ClerkProvider>
│   ├── App.tsx             # The broker logic — main porting target
│   └── vite-env.d.ts       # Typed import.meta.env
├── test-flow.js            # Loopback receiver, simulates the CLI side
├── .env.example
├── .gitignore
└── README.md
```

## Limitations

- No build / deploy story — `vite build` works but there's no point shipping this artefact; the page belongs in the Pluto webapp.
- No tests beyond `test-flow.js` (manual end-to-end).
- Doesn't enforce a "Connected CLIs" audit trail — the RFC's optional settings page is out of scope here (would need backend persistence).
