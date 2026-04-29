"""Browser-based broker login for pluto-judge.

Sign-in goes through `${PLUTO_API_BASE}/cli-auth` — a webapp page hosted
behind the Pluto Clerk session middleware that mints a session-template JWT
(`aud: svc:pluto-agent`) and POSTs it to a CLI loopback in the background.
The page itself renders the success/error UI; nothing about the JWT ever
appears in a URL, browser history, or referrer header. See RFC 0001
(`docs/rfcs/0001-web-broker-cli-auth.md`).

Self-contained — can be run standalone for testing:

    python src/auth.py login
    python src/auth.py status
    python src/auth.py logout

When imported, callers use `pluto_headers()` / `agent_headers()` to get an
`Authorization` header backed by a fresh JWT. In broker mode both header
builders return the same `aud: svc:pluto-agent` JWT — the broker mints a
single template, so the Pluto API gateway must accept that audience for
both the `/api/pluto/*` and CopilotKit agent surfaces. (The chrome backend
mints two distinct audience tokens.) `get_token()` re-runs the browser
flow inline if the cached JWT is expired; `force_login()` is the
programmatic re-auth entry point used by the server's 401-retry path.
"""

import base64
import http.server
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from urllib.parse import quote, urlparse

# Webapp host. Broker page lives at `${PLUTO_API_BASE}/cli-auth`. API tools
# in `src/server.py` derive their base URLs from the same env var so a single
# override flips both surfaces together.
PLUTO_API_BASE = os.environ.get("PLUTO_API_BASE", "https://pluto.stg.plurai.ai")

CRED_PATH = os.path.expanduser(
    os.environ.get("PLUTO_CREDENTIALS_PATH", "~/.config/pluto/credentials.json")
)

# Loopback ports the CLI tries (in order) for the broker redirect. MUST stay
# aligned with `ALLOWED_PORTS` in test/cli-auth-broker/src/validateParams.ts
# and the `connect-src` allowlist in test/cli-auth-broker/index.html (CSP).
REGISTERED_PORTS = (8765, 8766, 8767, 8768)

_token_lock = threading.Lock()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_jwt_payload(jwt: str) -> dict:
    try:
        decoded = json.loads(_b64url_decode(jwt.split(".")[1]).decode())
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _save_credentials(creds):
    os.makedirs(os.path.dirname(CRED_PATH), mode=0o700, exist_ok=True)
    fd = os.open(CRED_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f, indent=2)


def _load_credentials():
    if not os.path.exists(CRED_PATH):
        return None
    try:
        with open(CRED_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"WARNING: Could not load credentials at {CRED_PATH} ({e}); "
            "treating as not logged in.",
            file=sys.stderr,
        )
        return None


def get_token():
    """Return a valid JWT. Re-runs the browser flow if the cached token is
    expired — usually silent because the Clerk session cookie is still alive."""
    with _token_lock:
        creds = _load_credentials()
        if creds and creds.get("expires_at", 0) > time.time() + 60:
            return creds["access_token"]
        return _login_unlocked()["access_token"]


def force_login():
    """Programmatic re-auth. Always opens the browser. Raises on failure.

    Used by the server's 401-retry path to invalidate any cached/stale token
    and pop a fresh broker round-trip. The token lock serialises concurrent
    callers, so only one browser flow runs at a time."""
    with _token_lock:
        return _login_unlocked()


def pluto_headers():
    return {"Authorization": f"Bearer {get_token()}"}


def agent_headers():
    return {"Authorization": f"Bearer {get_token()}"}


# ── Browser flow ─────────────────────────────────────────────────────────


def _broker_origin() -> str:
    parsed = urlparse(PLUTO_API_BASE)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(
            f"PLUTO_API_BASE must be an absolute URL: {PLUTO_API_BASE!r}"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


# Cap the JSON body the broker POSTs to a few KB. A real session-template JWT
# is ~600–900 bytes; anything larger is malformed and we'd rather fail fast.
_MAX_POST_BYTES = 8 * 1024


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """One-shot loopback handler for the broker handoff.

    The broker page (cross-origin, served from PLUTO_API_BASE) POSTs the
    JWT here as JSON. We never accept the JWT via GET — putting it in a URL
    would expose it to browser history, address-bar shoulder-surfing, and
    any referrer leakage from the navigation. Two layers of access control:

    - CORS `Origin` allowlist (single value derived from PLUTO_API_BASE) —
      the browser will refuse to send a cross-origin POST without our
      explicit OK in the preflight response.
    - `state` parameter — generated fresh per login, known only to this
      process and the broker page we just opened.
    """

    captured: dict | None = None
    expected_state: str | None = None
    allowed_origin: str = ""

    def do_OPTIONS(self) -> None:
        if self.path != "/callback":
            self._empty(404)
            return
        origin = self.headers.get("Origin", "")
        if not self.allowed_origin or origin != self.allowed_origin:
            self._empty(403)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "60")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/callback":
            self._empty(404)
            return
        origin = self.headers.get("Origin", "")
        if not self.allowed_origin or origin != self.allowed_origin:
            self._json(403, {"error": "forbidden_origin"}, allow_origin=False)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "invalid_body"})
            return
        if length <= 0 or length > _MAX_POST_BYTES:
            self._json(400, {"error": "invalid_body"})
            return

        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "invalid_body"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "invalid_body"})
            return

        if payload.get("state") != _CallbackHandler.expected_state:
            _CallbackHandler.captured = {"error": "state_mismatch"}
            self._json(400, {"error": "state_mismatch"})
            return

        if "error" in payload:
            err = str(payload.get("error") or "unknown_error")
            _CallbackHandler.captured = {"error": err}
            self._json(200, {"ok": True})
            return

        token = payload.get("token")
        if not isinstance(token, str) or not token:
            _CallbackHandler.captured = {"error": "no_token"}
            self._json(400, {"error": "no_token"})
            return

        _CallbackHandler.captured = {
            "token": token,
            "expires_at": payload.get("expires_at"),
        }
        self._json(200, {"ok": True})

    def do_GET(self) -> None:
        # No tokens are accepted via GET. Keep responses content-free so a
        # mistakenly-pasted loopback URL can't render attacker-controlled
        # text in the user's browser.
        self._empty(404)

    def _json(self, status: int, body: dict, *, allow_origin: bool = True) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if allow_origin and self.allowed_origin:
            self.send_header("Access-Control-Allow-Origin", self.allowed_origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(encoded)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args: object, **_kwargs: object) -> None:
        return  # silence default access log


def _login_unlocked():
    """Run the broker flow, persist new credentials, return the dict.
    Caller must hold `_token_lock`."""
    state = secrets.token_urlsafe(16)

    server = None
    for port in REGISTERED_PORTS:
        try:
            server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError(
            f"All registered loopback ports {REGISTERED_PORTS} are in use. "
            "Free one and retry."
        )

    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
    _CallbackHandler.expected_state = state
    _CallbackHandler.captured = None
    _CallbackHandler.allowed_origin = _broker_origin()

    auth_url = (
        f"{PLUTO_API_BASE}/cli-auth"
        f"?redirect_uri={quote(redirect_uri, safe='')}"
        f"&state={state}"
    )

    # Stdout is reserved for MCP JSON-RPC framing when this runs from the
    # server's 401-retry path; route all user-facing messages to stderr.
    print("Opening browser to log in...", file=sys.stderr)
    print(
        f"If the browser doesn't open, paste this URL:\n  {auth_url}\n",
        file=sys.stderr,
    )
    webbrowser.open(auth_url)

    server.timeout = 1
    deadline = time.time() + 300  # 5-minute window for the user to complete login
    while _CallbackHandler.captured is None and time.time() < deadline:
        server.handle_request()
    server.server_close()

    captured = _CallbackHandler.captured
    if not captured:
        raise RuntimeError("Timed out waiting for browser redirect.")
    if "error" in captured:
        raise RuntimeError(f"Login failed: {captured['error']}")

    token = captured["token"]
    payload = _decode_jwt_payload(token)

    expires_at: int | None = None
    raw_exp = captured.get("expires_at")
    if raw_exp is not None:
        try:
            expires_at = int(raw_exp)
        except (TypeError, ValueError):
            expires_at = None
    if expires_at is None:
        exp_claim = payload.get("exp")
        if isinstance(exp_claim, (int, float)):
            expires_at = int(exp_claim)
    # Refuse to persist a token without a real expiry. expires_at=0 would
    # cause `get_token` to re-pop the browser on every subsequent call.
    if expires_at is None or expires_at <= time.time():
        raise RuntimeError(
            "Broker returned a JWT without a valid future expiry; aborting login."
        )

    creds = {
        "access_token": token,
        "expires_at": expires_at,
        # Best-effort: present only if the Clerk JWT template emits an `email`
        # claim. Falls back to empty string for `auth status` display.
        "email": payload.get("email", ""),
    }
    _save_credentials(creds)
    return creds


# ── CLI subcommands (login / logout / status) ────────────────────────────


def login():
    try:
        creds = force_login()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Logged in as {creds['email'] or '<unknown>'}.")
    print(f"Credentials stored at {CRED_PATH}.")
    return 0


def logout():
    if not os.path.exists(CRED_PATH):
        print("Not logged in.")
        return 0
    try:
        os.unlink(CRED_PATH)
    except OSError as e:
        print(
            f"Failed to delete credentials at {CRED_PATH}: {e}",
            file=sys.stderr,
        )
        return 1
    print("Logged out.")
    return 0


def status():
    creds = _load_credentials()
    if not creds:
        print("Not logged in.")
        return 1
    print(f"Logged in as {creds.get('email') or '<unknown>'}.")
    print(f"Token expires: {time.ctime(creds.get('expires_at', 0))}")
    print(f"Credentials at: {CRED_PATH}")
    return 0


def main(argv=None):
    """CLI entry point. argv is the list of args after the `auth` keyword."""
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "status"
    if cmd == "login":
        return login()
    if cmd == "logout":
        return logout()
    if cmd == "status":
        return status()
    print(f"Unknown auth command: {cmd}. Use: login, logout, status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
