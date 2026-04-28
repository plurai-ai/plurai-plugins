# RFC 0001 — Web Broker CLI authentication

**Status:** Draft
**Author:** benw@plurai.ai
**Date:** 2026-04-27
**Reviewers (TBA):** Pluto backend lead, CLI maintainer

## 1. Summary

Replace the CLI's Clerk OAuth 2.0 Application + PKCE flow with a single
HTML page on the Pluto webapp that acts as a token broker. The page sits
behind the existing Clerk session middleware, calls
`Clerk.session.getToken({template: 'pluto-agent-authz'})` in the browser, and
`fetch`-POSTs the resulting JWT to a CLI loopback in the background while
itself rendering the success/error UI. The CLI uses the JWT as
`Authorization: Bearer …` for API calls. **No new backend endpoints, no
DB changes, no new auth-verifier work.** When the JWT expires (10 min),
the CLI re-runs the broker flow on the next 401 — silent to the user as
long as the Clerk browser session is still alive.

## 2. Background

`src/auth.py` currently performs Clerk OAuth 2.0 Application + PKCE against
`clerk.stg.plurai.ai` with `client_id=Q8PUrXzINxDoqtnH`. The resulting access
token is rejected by the Pluto API with `401 Unauthorized`. Side-by-side decode
of an OAuth-flow token vs. the `__session` cookie used by the web app:

| claim    | OAuth-flow token (401s)                | Browser session token (works)               |
|----------|-----------------------------------------|----------------------------------------------|
| `typ`    | `at+jwt` (RFC 9068)                     | `JWT`                                        |
| `aud`    | _missing_                               | `svc:pluto-agent`                            |
| `azp`    | _missing_                               | `https://pluto.stg.plurai.ai`               |
| `kind`   | _missing_                               | `user`                                       |
| `oid`    | _missing_                               | `org_3Cw3w5zPMfsIc39PLYNHxEXJibd`            |
| `sub`    | `user_3AeHTI...` (OAuth-app namespace)  | `user_3Cw3w6Q...` (regular user namespace)   |
| lifetime | 24 h                                    | 10 min                                       |

Two issues compound:

1. **Audience.** The Pluto API gates on `aud == svc:pluto-agent`. Clerk's default
   OAuth-Application access tokens carry no `aud`.
2. **User-namespace fork.** Clerk OAuth Applications create their own user
   records distinct from the regular user table — `sub` differs even for the
   same email.

The web app already produces correctly-shaped tokens via Clerk JS's session
template feature. The simplest fix is to let the CLI piggyback on that exact
mechanism through a thin loopback handoff.

The 8 call sites in `src/server.py`
(`168, 179, 238, 253, 334, 508, 552, 594`) all go through `pluto_headers()` /
`agent_headers()` from `src/auth.py`, so the change is fully contained behind
that boundary.

## 3. Goals & non-goals

### Goals

- The CLI authenticates as the **same user record** the web app uses (matching `sub` and `oid`).
- Tokens emitted to the API match the existing verifier's expectations — **no API verifier changes**.
- **No new backend endpoints, no DB schema, no new persistence on Pluto's side.**
- Standard CLI UX: one-time `auth login`, automatic silent re-auth as long as the Clerk browser session is alive.

### Non-goals

- Headless / CI authentication (no browser available). A future RFC can add service-account credentials.
- Per-org or per-project scoped credentials. The minted token carries the user's full permissions in their currently-active org.
- Eliminating browser pop-ups for long-running operations (see §6).

## 4. Proposed design

### Browser flow

1. CLI generates `state = secrets.token_urlsafe(16)`.
2. CLI binds a loopback HTTP server to one of `REGISTERED_PORTS = (8765, 8766, 8767, 8768)`. Handler accepts `OPTIONS /callback` (CORS preflight) and `POST /callback` (JSON handoff). **No GET path accepts a token** — this is intentional, see §8.
3. CLI opens the user's browser to:
   ```
   ${PLUTO_API_BASE}/cli-auth?redirect_uri=http://127.0.0.1:PORT/callback&state=<state>
   ```
4. The Pluto webapp serves `/cli-auth` behind its existing Clerk session middleware (sign-in → bounce-back if not authenticated).
5. The page:
   - Validates `redirect_uri` against the allowlist (loopback only).
   - Calls `Clerk.session.getToken({template: 'pluto-agent-authz'})` — this is the same call the regular web app already makes for its own API requests.
   - Reads `exp` from the JWT.
   - **`fetch`-POSTs `{state, token, expires_at}` as JSON** to `${redirect_uri}` (`mode: 'cors'`, `credentials: 'omit'`, `referrerPolicy: 'no-referrer'`).
   - Renders an in-page "Logged in. You can close this tab." status — the browser never navigates with the JWT.
6. CLI loopback handler verifies `Origin` matches the broker, validates `state`, captures `token` + `expires_at`, replies `200 {ok: true}` (with the CORS allowlist headers), and `server_close()`s.
7. CLI persists `{access_token, expires_at, email}` to `~/.config/pluto/credentials.json` (`0600`).

### Token use

- `pluto_headers()` / `agent_headers()` read the persisted token. If `expires_at` is in the future (with a 60 s skew margin), use it.
- If expired, or if any API call returns `401`, the CLI **re-runs the browser flow inline** (see §6).
- Same-process concurrent calls coordinate through a `threading.Lock` so only one browser window opens per refresh.

## 5. Broker contract

Pluto webapp adds **one** surface. Hosts: `https://pluto.plurai.ai` (prod), `https://pluto.stg.plurai.ai` (staging).

### `GET /cli-auth?redirect_uri&state`

- **Auth:** existing Clerk session middleware. If unauthenticated → Clerk sign-in → bounce back to `/cli-auth` with the same query string.
- **Validation:** `redirect_uri` MUST be `http://127.0.0.1:<allowed-port>/callback` or `http://localhost:<allowed-port>/callback` for `port ∈ REGISTERED_PORTS`. Reject otherwise with a 400 page.
- **Behavior:** call `Clerk.session.getToken({template: 'pluto-agent-authz'})`, then `fetch`-POST `{state, token, expires_at}` as JSON to `redirect_uri`. Render the result inline; never navigate the browser with the JWT.
- **No persistence** beyond what Clerk already does for the user's session.

That's the entire backend delta. No `/api/cli-auth/*` endpoints, no DB rows, no new tables.

## 6. Re-login on 401

The minted JWT lives ~10 minutes. When it expires:

1. Next API call from the CLI returns 401.
2. The HTTP wrapper in `src/auth.py` catches the 401, acquires the refresh lock, and re-invokes `login()` — same loopback dance, browser opens.
3. Because the Clerk browser session is still alive (typically 1–7 days, instance-configured), the page redirects through `/cli-auth` and back to localhost in ~1 second with **no user interaction**. A tab briefly opens and closes itself ("Logged in. You can close this tab.").
4. The HTTP wrapper retries the original request with the new token.
5. Once per Clerk session lifetime, the user actually has to type a password.

**Acceptable awkwardness:** a long-running tool call that exceeds the JWT lifetime (e.g. `pluto_get_results` polling SLM optimization for ~20 min) will pop a browser tab mid-call. Tab self-closes silently in the common case. We accept this as the price of avoiding backend complexity.

**Concurrency:** a single `threading.Lock` in `auth.py` ensures only one re-login flow runs at a time, even if multiple MCP tool calls hit 401 simultaneously. Other waiters block on the lock and reuse the freshly-minted token.

**Failure modes:**

- Clerk session expired → page shows Clerk sign-in inside the popped tab; user signs in; flow completes. Worst case: an inline tool call fails with a "Login expired, please try again" message and the user re-runs whatever they were doing.
- All `REGISTERED_PORTS` busy → tool call fails with the existing "free a port and retry" error (`src/auth.py:196`).
- Browser unavailable (e.g. user is on SSH without DISPLAY forwarding) → fails; headless support is out of scope for this RFC.

## 7. CLI changes (deferred to implementation phase)

This RFC does not ship code. A follow-up plan will:

- Rewrite `src/auth.py`:
  - Keep: `pluto_headers()`, `agent_headers()`, `get_token()`, `_save_credentials`, `_load_credentials`, `_CallbackHandler`, `REGISTERED_PORTS` scaffolding, `auth.main(argv)` (`login` / `logout` / `status`), `_form_post`.
  - Drop: `_gen_pkce`, `_refresh_token`, `CLIENT_ID`, `CLERK_AUTHORIZE_URL`, `CLERK_TOKEN_URL`, `CLERK_USERINFO_URL`, `CLERK_REVOKE_URL`, the `PLUTO_CLERK_CLIENT_ID` env override.
  - Add: a `_with_auth_retry(fn)` wrapper that retries on 401 by invoking `login()` under a module-level lock. Wire it into `pluto_headers` / `agent_headers` (or into the request helpers in `src/server.py:96-126`).
  - Defaults: `PLUTO_API_BASE = os.environ.get("PLUTO_API_BASE", "https://pluto.plurai.ai")`.
- No changes to:
  - `src/server.py` — call sites stay as-is, with the retry wrapper applied transparently.
  - `hooks/check-auth.sh` — still checks file presence at `~/.config/pluto/credentials.json`.
  - `commands/login.md` — still invokes `${CLAUDE_PLUGIN_ROOT}/run.sh auth login`.
  - `.mcp.json`, `.claude-plugin/plugin.json`.

## 8. Security

- **Token never appears in a URL.** The broker hands the JWT to the CLI via a background `fetch` POST with a JSON body, not a 302 redirect. Consequence: no browser-history entry, no address-bar exposure, no leakage via `Referer` on the navigation, no token surviving in `location.href` if the CLI tab is later inspected. This is the central hardening over the original RFC sketch.
- **Token at rest (CLI):** `~/.config/pluto/credentials.json`, file `0600`, parent dir `0700`. Token is a ~10-min JWT — short-lived even if leaked.
- **Loopback:** bind to `127.0.0.1` only; one-shot handler accepts `OPTIONS` and `POST` on `/callback` only; `GET` returns 404 with no body.
- **Loopback authentication, two layers:**
  - CORS `Origin` allowlist: handler responds to the preflight only when `Origin` equals `${PLUTO_API_BASE}` exactly. Browsers refuse to send the cross-origin POST without this OK.
  - `state` parameter generated per login, known only to this CLI process and the broker page it just opened.
- **Redirect-URI allowlist (server-side):** only `http://127.0.0.1:<port>/callback` and/or `http://localhost:<port>/callback` for `port ∈ REGISTERED_PORTS`.
- **CSP `connect-src`:** the broker page's CSP must list each loopback origin (`http://127.0.0.1:<port>` and `http://localhost:<port>` for every registered port) so the handoff `fetch` is allowed. `form-action 'none'` and `base-uri 'none'` close off other ways the page could exfiltrate the token.
- **No long-lived secrets on disk** — worst-case credential leakage window is 10 minutes (JWT lifetime).
- **Logout:** `auth logout` deletes the local credentials file. Server-side has nothing to revoke (no persistence).
- **Token theft mitigation:** because the broker only mints session-template tokens (the same ones the regular web app produces), Clerk's existing rate-limits, anomaly detection, and session controls all apply uniformly. No new attack surface.

## 9. Migration / rollout

The current OAuth Application flow is non-functional, so there's no working state to preserve. Old `credentials.json` files are silently overwritten on next `auth login`.

Suggested sequence:

1. Pluto webapp ships `/cli-auth` on staging.
2. CLI implementation PR lands gated on `PLUTO_API_BASE=https://pluto.stg.plurai.ai`.
3. Internal dogfooding for a few days.
4. Pluto webapp ships `/cli-auth` on prod.
5. CLI default flips to prod (no code change — prod is already the default per `PLUTO_API_BASE`).
6. Old Clerk OAuth Application can be deleted from the Clerk dashboard.

## 10. Alternatives considered

### Configure the Clerk OAuth App audience

Clerk supports adding an `aud` claim to OAuth-App tokens via JWT templates. Fixes
the audience but **not the user-namespace fork** (`sub` still differs) or the
missing `oid`. Pluto API would have to learn to dual-resolve identities. Rejected.

### Backend-issued refresh credential

CLI receives a long-lived opaque refresh token; backend exposes
`/api/cli-auth/{issue,exchange,revoke}` endpoints; CLI exchanges refresh for
fresh JWTs as needed. Eliminates the mid-tool-call browser pop, at the cost of
new endpoints, a credentials table, rotation/theft-detection logic, and a
"Connected CLIs" settings UI. Considered but rejected for v1 — strictly more
complex than the broker page. **Reconsider if the mid-call browser pop turns
out to be too disruptive in practice.**

### Long-lived API keys via `pluto_create_api_key`

Existing API keys are scoped to **deployed evaluator endpoints**, not the
management API. Adding a parallel "user API key" surface is more backend work
than the broker, with non-standard CLI UX. Rejected.

### Backend changes to verifier to accept OAuth-App tokens

Touches every `Authorization: Bearer` check in the API, and still leaves the
user-namespace problem. Rejected.

## 11. Open questions

1. **Consent screen vs. instant redirect.** Should `/cli-auth` show a one-time "Authorize this CLI?" prompt, or just immediately mint and redirect? The user reached this page deliberately by running `auth login`, so a consent step is somewhat redundant. Recommend: skip consent for v1; revisit if we ever support a CLI flow that doesn't start with `auth login`.
2. **Clerk template name on prod.** Confirmed on staging: template is `pluto-agent-authz` (verified via DevTools — `/v1/client/sessions/<sid>/tokens/pluto-agent-authz`). Confirm the same template name exists and is enabled on the prod Clerk instance.
3. **Loopback port allowlist.** Today the CLI rotates through `(8765, 8766, 8767, 8768)`. Does the server-side allowlist need to match exactly, or accept any localhost port? Tighter (fixed allowlist) is safer; looser is more flexible if we add CLI ports later.
4. **`expires_at` in redirect URL.** Including the JWT `exp` in the query string lets the CLI cache without parsing the JWT. Alternative: have the CLI base64-decode the JWT itself. No strong preference — recommend including it for simplicity.

## 12. Decisions (filled in during review)

_To be populated as open questions are resolved._
