"""Browser-based OAuth 2.0 + PKCE login flow against Clerk for pluto-judge.

Self-contained — can be run standalone for testing:

    python src/auth.py login
    python src/auth.py status
    python src/auth.py logout

When imported, callers use `pluto_headers()` / `agent_headers()` to get an
`Authorization` header backed by a fresh Clerk OAuth access token.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import ssl
import sys
import threading
import time
import webbrowser
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

# Clerk OAuth 2.0 endpoints (https://clerk.plurai.ai/.well-known/openid-configuration).
CLERK_ISSUER = "https://clerk.stg.plurai.ai"
CLERK_AUTHORIZE_URL = f"{CLERK_ISSUER}/oauth/authorize"
CLERK_TOKEN_URL = f"{CLERK_ISSUER}/oauth/token"
CLERK_USERINFO_URL = f"{CLERK_ISSUER}/oauth/userinfo"
CLERK_REVOKE_URL = f"{CLERK_ISSUER}/oauth/token/revoke"

# Public Clerk OAuth Application client_id for pluto-judge. Set this constant once an OAuth
# Application has been created in dashboard.clerk.com → Pluto instance → OAuth Applications.
# Public client (no secret) — PKCE is mandatory. Override via env var for staging/dev.
CLIENT_ID = os.environ.get("PLUTO_CLERK_CLIENT_ID", "Q8PUrXzINxDoqtnH")

CRED_PATH = os.path.expanduser(
    os.environ.get("PLUTO_CREDENTIALS_PATH", "~/.config/pluto/credentials.json")
)

_SSL_CTX = ssl.create_default_context()
_token_lock = threading.Lock()

# Cloudflare in front of Clerk's dev instances blocks the default `Python-urllib/...`
# UA as a bot. Send a real-looking UA on every Clerk call.
_USER_AGENT = "pluto-judge/0.1 (+https://pluto.plurai.ai)"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _gen_pkce():
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _form_post(url, data):
    body = urlencode(data).encode()
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", _USER_AGENT)
    with urlopen(req, timeout=30, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode())


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


def _refresh_token(creds):
    if not creds.get("refresh_token"):
        raise RuntimeError("Not logged in. Run: npx pluto-judge auth login")
    resp = _form_post(CLERK_TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
        "client_id": CLIENT_ID,
    })
    creds["access_token"] = resp["access_token"]
    creds["expires_at"] = int(time.time() + int(resp.get("expires_in", 3600)))
    if "refresh_token" in resp:
        # Clerk may rotate refresh tokens; persist the new one if so.
        creds["refresh_token"] = resp["refresh_token"]
    _save_credentials(creds)
    return creds


def get_token():
    """Return a valid Clerk OAuth access token. Refreshes silently when expiring soon."""
    with _token_lock:
        creds = _load_credentials()
        if not creds:
            raise RuntimeError("Not logged in. Run: npx pluto-judge auth login")
        if creds.get("expires_at", 0) < time.time() + 60:
            try:
                creds = _refresh_token(creds)
            except HTTPError as e:
                if e.code in (400, 401):
                    raise RuntimeError("Login expired. Run: npx pluto-judge auth login") from e
                raise
        return creds["access_token"]


def pluto_headers():
    return {"Authorization": f"Bearer {get_token()}"}


def agent_headers():
    return {"Authorization": f"Bearer {get_token()}"}


# ── CLI subcommands (login / logout / status) ────────────────────────────


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """One-shot loopback handler for the OAuth redirect."""

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
            self._reply(400, f"Auth failed: {params.get('error_description', params['error'])}")
            _CallbackHandler.captured = {"error": params["error"]}
            return
        if "code" not in params:
            self._reply(400, "No code in response.")
            _CallbackHandler.captured = {"error": "no_code"}
            return
        _CallbackHandler.captured = {"code": params["code"]}
        self._reply(200, "Logged in. You can close this tab.")

    def _reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<html><body><h2>{body}</h2></body></html>".encode())

    def log_message(self, *args, **kwargs):
        return  # silence default access log


def login():
    if not CLIENT_ID:
        print(
            "Clerk OAuth client_id not configured.\n"
            "Create an OAuth Application at dashboard.clerk.com (Pluto instance → OAuth Applications),\n"
            "then set CLIENT_ID in src/auth.py or export PLUTO_CLERK_CLIENT_ID.",
            file=sys.stderr,
        )
        return 1

    verifier, challenge = _gen_pkce()
    state = secrets.token_urlsafe(16)

    # Clerk requires exact-match redirect URIs (no port wildcards), so we use
    # a small registered set and try each in order. All of these must be added
    # to the OAuth Application's allowed Redirect URIs in the Clerk dashboard.
    REGISTERED_PORTS = (8765, 8766, 8767, 8768)
    server = None
    for port in REGISTERED_PORTS:
        try:
            server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
            break
        except OSError:
            continue
    if server is None:
        print(
            f"All registered loopback ports {REGISTERED_PORTS} are in use. "
            "Free one and retry, or register additional ports in Clerk and add them here.",
            file=sys.stderr,
        )
        return 1
    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
    _CallbackHandler.expected_state = state
    _CallbackHandler.captured = None

    auth_url = CLERK_AUTHORIZE_URL + "?" + urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "openid profile email offline_access",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

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
        print("Timed out waiting for browser redirect.", file=sys.stderr)
        return 1
    if "error" in captured:
        print(f"Login failed: {captured['error']}", file=sys.stderr)
        return 1

    try:
        token_resp = _form_post(CLERK_TOKEN_URL, {
            "grant_type": "authorization_code",
            "code": captured["code"],
            "redirect_uri": redirect_uri,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        })
    except HTTPError as e:
        body = e.read().decode() if e.fp else str(e)
        print(f"Token exchange failed (HTTP {e.code}): {body}", file=sys.stderr)
        return 1

    creds = {
        "access_token": token_resp["access_token"],
        "refresh_token": token_resp.get("refresh_token"),
        "expires_at": int(time.time() + int(token_resp.get("expires_in", 3600))),
        "token_type": token_resp.get("token_type", "Bearer"),
        "scope": token_resp.get("scope", ""),
    }

    try:
        req = Request(CLERK_USERINFO_URL)
        req.add_header("Authorization", f"Bearer {creds['access_token']}")
        req.add_header("User-Agent", _USER_AGENT)
        with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            info = json.loads(resp.read().decode())
        creds["email"] = info.get("email", "")
    except (HTTPError, OSError):
        creds["email"] = ""

    _save_credentials(creds)
    print(f"Logged in as {creds['email'] or '<unknown>'}.")
    print(f"Credentials stored at {CRED_PATH}.")
    return 0


def logout():
    creds = _load_credentials()
    if not creds:
        print("Not logged in.")
        return 0
    if creds.get("refresh_token") and CLIENT_ID:
        try:
            _form_post(CLERK_REVOKE_URL, {
                "token": creds["refresh_token"],
                "client_id": CLIENT_ID,
            })
        except (HTTPError, OSError):
            pass  # best-effort revocation; the local file is what really matters
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
