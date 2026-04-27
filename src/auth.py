"""Browser-based broker login for pluto-judge.

Sign-in goes through `${PLUTO_API_BASE}/cli-auth` — a webapp page hosted
behind the Pluto Clerk session middleware that mints a session-template JWT
(`aud: svc:pluto-agent`) and 302s it to a CLI loopback redirect. See RFC
0001 (`docs/rfcs/0001-web-broker-cli-auth.md`).

Self-contained — can be run standalone for testing:

    python src/auth.py login
    python src/auth.py status
    python src/auth.py logout

When imported, callers use `pluto_headers()` / `agent_headers()` to get an
`Authorization` header backed by a fresh JWT. `get_token()` re-runs the
browser flow inline if the cached JWT is expired; `force_login()` is the
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
from urllib.parse import parse_qs, quote, urlparse

# Webapp host. Broker page lives at `${PLUTO_API_BASE}/cli-auth`. API tools
# in `src/server.py` derive their base URLs from the same env var so a single
# override flips both surfaces together.
PLUTO_API_BASE = os.environ.get("PLUTO_API_BASE", "https://pluto.stg.plurai.ai")

CRED_PATH = os.path.expanduser(
    os.environ.get("PLUTO_CREDENTIALS_PATH", "~/.config/pluto/credentials.json")
)

# Loopback ports the CLI tries (in order) for the broker redirect. MUST stay
# aligned with VITE_ALLOWED_REDIRECT_PORTS in test/cli-auth-broker/.env.example
# and the eventual server-side allowlist on the Pluto webapp.
REGISTERED_PORTS = (8765, 8766, 8767, 8768)

_token_lock = threading.Lock()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_jwt_payload(jwt: str) -> dict:
    try:
        return json.loads(_b64url_decode(jwt.split(".")[1]).decode())
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


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
    except (OSError, json.JSONDecodeError):
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


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """One-shot loopback handler for the broker redirect."""

    captured: dict | None = None
    expected_state: str | None = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if params.get("state") != _CallbackHandler.expected_state:
            self._reply(400, "State mismatch.")
            _CallbackHandler.captured = {"error": "state_mismatch"}
            return
        if "error" in params:
            self._reply(
                400, f"Auth failed: {params.get('error_description', params['error'])}"
            )
            _CallbackHandler.captured = {"error": params["error"]}
            return
        if "token" not in params:
            self._reply(400, "No token in response.")
            _CallbackHandler.captured = {"error": "no_token"}
            return
        _CallbackHandler.captured = {
            "token": params["token"],
            "expires_at": params.get("expires_at"),
        }
        self._reply(200, "Logged in. You can close this tab.")

    def _reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<html><body><h2>{body}</h2></body></html>".encode())

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

    auth_url = (
        f"{PLUTO_API_BASE}/cli-auth"
        f"?redirect_uri={quote(redirect_uri, safe='')}"
        f"&state={state}"
    )

    print("Opening browser to log in...")
    print(f"If the browser doesn't open, paste this URL:\n  {auth_url}\n")
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
    if raw_exp:
        try:
            expires_at = int(raw_exp)
        except ValueError:
            expires_at = None
    if not expires_at:
        exp_claim = payload.get("exp")
        expires_at = int(exp_claim) if isinstance(exp_claim, (int, float)) else 0

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
    except OSError:
        pass
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
