"""Chrome-cookie auth for pluto-judge.

Reads the user's existing Pluto session straight out of the local Chrome
cookie store and exchanges it for a Pluto JWT via the Clerk Frontend
API. Currently the default backend (selected by the dispatcher in
`auth.py`) while the web-broker flow in `auth_broker.py` is still being
wired up — set `PLUTO_AUTH_METHOD=broker` to opt back into the broker.

Same public API as `auth_broker.py`: `get_token`, `force_login`,
`pluto_headers`, `agent_headers`, `main`. `pluto_headers` uses the
default Clerk session JWT (Pluto API audience); `agent_headers` mints
a separate template JWT for the CopilotKit agent endpoint.

Self-contained — can be run standalone for testing:

    PLUTO_AUTH_METHOD=chrome python src/auth.py status
    PLUTO_AUTH_METHOD=chrome python src/auth.py login

Requirements:
- macOS (Chrome cookie paths are hardcoded to ~/Library/...)
- `openssl` on PATH
- A live Pluto session — log in once at https://pluto.plurai.ai

The Chrome safe-storage seed is read automatically from the macOS keychain
entry "Chrome Safe Storage" via `security find-generic-password`. Set
`CHROME_SAFE_STORAGE` to override (useful in CI or non-keychain setups).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PLUTO_ORIGIN = os.environ.get("PLUTO_API_BASE", "https://pluto.plurai.ai").rstrip("/")
PLUTO_API = f"{PLUTO_ORIGIN}/api/pluto"
# Clerk FAPI mirrors the Pluto host (prod ↔ clerk.plurai.ai,
# stg ↔ clerk.stg.plurai.ai). Override with CLERK_FAPI if needed.
CLERK_FAPI = os.environ.get(
    "CLERK_FAPI",
    PLUTO_ORIGIN.replace("://pluto.", "://clerk.").rstrip("/") + "/v1",
)

_SSL_CTX = ssl.create_default_context()
_token_lock = threading.Lock()
_token_cache: tuple[str, float] | None = None  # (jwt, expire_time)
_agent_token_cache: tuple[str, float] | None = None  # (jwt, expire_time)
_chrome_key_cache: bytes | None = None
AGENT_TOKEN_TEMPLATE = "pluto-agent-authz"


def _read_safe_storage() -> str:
    """Resolve the Chrome safe-storage seed.

    Prefers the explicit `CHROME_SAFE_STORAGE` env var; falls back to the
    macOS keychain entry "Chrome Safe Storage". The first keychain read
    may show a one-time access prompt — choosing "Always Allow" makes it
    silent thereafter.
    """
    explicit = os.environ.get("CHROME_SAFE_STORAGE", "")
    if explicit:
        return explicit
    if sys.platform != "darwin":
        return ""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Chrome Safe Storage", "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _chrome_key() -> bytes | None:
    """Derive the AES-128 key from the safe-storage seed (cached)."""
    global _chrome_key_cache
    if _chrome_key_cache is not None:
        return _chrome_key_cache
    seed = _read_safe_storage()
    if not seed:
        return None
    _chrome_key_cache = hashlib.pbkdf2_hmac(
        "sha1", seed.encode(), b"saltysalt", 1003, dklen=16
    )
    return _chrome_key_cache


# ── JWT helpers ──────────────────────────────────────────────────────────


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_jwt_payload(jwt: str) -> dict:
    try:
        decoded = json.loads(_b64url_decode(jwt.split(".")[1]).decode())
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


# ── Chrome cookie reader ─────────────────────────────────────────────────


def _decrypt_chrome_cookie(enc_value: bytes) -> str | None:
    """Decrypt a Chrome v10-encrypted cookie value via the openssl CLI."""
    key = _chrome_key()
    if enc_value[:3] != b"v10" or key is None:
        return None
    iv = b" " * 16
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(enc_value[3:])
        f.flush()
        tmp_path = f.name
    try:
        result = subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-128-cbc",
                "-d",
                "-nopad",
                "-K",
                key.hex(),
                "-iv",
                iv.hex(),
                "-in",
                tmp_path,
            ],
            capture_output=True,
        )
    finally:
        os.unlink(tmp_path)
    dec = result.stdout
    if not dec:
        return None
    pad_len = dec[-1]
    if 0 < pad_len <= 16:
        dec = dec[:-pad_len]
    value = dec.decode("utf-8", errors="replace")
    # Find JWT start (after possible garbage prefix from block cipher).
    jwt_start = value.find("eyJ")
    return value[jwt_start:] if jwt_start >= 0 else value


_CHROME_PROFILES = ("Profile 1", "Default")


def _read_chrome_cookie(
    host_pattern: str, cookie_name: str, profile: str | None = None
) -> str | None:
    """Read a cookie from Chrome's cookie DB. Returns None if not found.

    `profile=None` searches all known profiles in order and returns the first
    match. Pass an explicit profile name to scope to one profile (used by the
    session-discovery flow, which needs to keep `__client` and the resulting
    session bound to the same profile).
    """
    profiles = (profile,) if profile else _CHROME_PROFILES
    for prof in profiles:
        db_path = os.path.expanduser(
            f"~/Library/Application Support/Google/Chrome/{prof}/Cookies"
        )
        if not os.path.exists(db_path):
            continue
        # Chrome holds an exclusive lock on the live DB; copy it first.
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        shutil.copy2(db_path, tmp)
        try:
            conn = sqlite3.connect(tmp)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT encrypted_value FROM cookies "
                    "WHERE host_key LIKE ? AND name = ? "
                    "ORDER BY expires_utc DESC LIMIT 1",
                    (host_pattern, cookie_name),
                )
                row = cur.fetchone()
            finally:
                conn.close()
            if row:
                return _decrypt_chrome_cookie(row[0])
        finally:
            os.unlink(tmp)
    return None


def _get_client_cookie_and_session_id() -> tuple[str | None, str | None]:
    """Return (`__client` JWT, currently-active session ID).

    Iterates Chrome profiles and returns the first one whose `__client` device
    cookie corresponds to an active Clerk session. Without this, a stale
    `__client` in one profile (e.g. Profile 1 left over from a previous
    sign-in) can shadow the real signed-in profile (Default), causing
    `/v1/client` to return zero sessions.
    """
    from urllib.parse import urlparse

    clerk_host = urlparse(CLERK_FAPI).netloc
    last_client_jwt: str | None = None
    for profile in _CHROME_PROFILES:
        client_jwt = _read_chrome_cookie(f"%{clerk_host}%", "__client", profile)
        if not client_jwt:
            continue
        last_client_jwt = client_jwt
        req = Request(f"{CLERK_FAPI}/client?_clerk_js_version=5.0.0")
        req.add_header("Origin", PLUTO_ORIGIN)
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Cookie", f"__client={client_jwt}")
        try:
            with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                body = json.loads(resp.read().decode())
        except (OSError, json.JSONDecodeError):
            continue
        sessions = body.get("response", {}).get("sessions") or []
        for s in sessions:
            if s.get("status") == "active" and isinstance(s.get("id"), str):
                return client_jwt, s["id"]
    # No profile yielded an active session; surface the device cookie we did
    # find (if any) so callers can produce a useful "log in" error message.
    return last_client_jwt, None


# ── Token retrieval ──────────────────────────────────────────────────────


def _mint_token(template: str | None) -> str:
    """Hit the Clerk Frontend API for a fresh session JWT.

    `template=None` returns the default session token (Pluto API audience).
    `template='pluto-agent-authz'` returns the agent-audience token. Caller
    must hold `_token_lock`.
    """
    if _chrome_key() is None:
        raise RuntimeError(
            "Could not resolve the Chrome safe-storage seed. On macOS the keychain "
            'entry "Chrome Safe Storage" is normally read automatically; if that '
            "fails, export CHROME_SAFE_STORAGE explicitly."
        )

    client_jwt, session_id = _get_client_cookie_and_session_id()
    if not session_id:
        raise RuntimeError(
            f"No active Pluto session found in Chrome. Log in at {PLUTO_ORIGIN} first."
        )

    suffix = f"/{template}" if template else ""
    url = f"{CLERK_FAPI}/client/sessions/{session_id}/tokens{suffix}?_clerk_js_version=5.0.0"
    req = Request(url, data=b"", method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Origin", PLUTO_ORIGIN)
    req.add_header("User-Agent", "Mozilla/5.0")
    if client_jwt:
        req.add_header("Cookie", f"__client={client_jwt}")

    with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
        body = json.loads(resp.read().decode())

    jwt = body.get("jwt") or body.get("response", {}).get("jwt")
    if not isinstance(jwt, str) or not jwt:
        raise RuntimeError("Clerk Frontend API returned no JWT.")
    return jwt


def _fetch_token_unlocked() -> str:
    """Default Pluto-API token. Caller must hold `_token_lock`."""
    global _token_cache
    if _token_cache and _token_cache[1] > time.time():
        return _token_cache[0]
    jwt = _mint_token(None)
    # Tokens are valid for ~60s; cache for 50s to leave a safety margin.
    _token_cache = (jwt, time.time() + 50)
    return jwt


def _fetch_agent_token_unlocked() -> str:
    """Agent-audience token (`aud == svc:pluto-agent`). Caller must hold `_token_lock`."""
    global _agent_token_cache
    if _agent_token_cache and _agent_token_cache[1] > time.time():
        return _agent_token_cache[0]
    jwt = _mint_token(AGENT_TOKEN_TEMPLATE)
    _agent_token_cache = (jwt, time.time() + 50)
    return jwt


def get_token() -> str:
    with _token_lock:
        return _fetch_token_unlocked()


def get_agent_token() -> str:
    with _token_lock:
        return _fetch_agent_token_unlocked()


def _label(payload: dict) -> str:
    """Best-effort identity string from a Clerk session JWT.

    Default Clerk session tokens have no `email` claim — only template
    tokens do. Fall back to org slug, then user ID, then '<unknown>'."""
    email = payload.get("email")
    if isinstance(email, str) and email:
        return email
    org = payload.get("o", {})
    slug = org.get("slg") if isinstance(org, dict) else None
    if isinstance(slug, str) and slug:
        return f"org:{slug}"
    sub = payload.get("sub")
    if isinstance(sub, str) and sub:
        return sub
    return "<unknown>"


def _verify_pluto_api(token: str) -> None:
    """Probe the Pluto API to confirm it accepts the token. Raises on failure.

    Without this, `auth login` only proves Clerk minted *some* JWT — it
    doesn't prove the Pluto backend will accept it. A 200 from /threads
    means the full auth chain works end-to-end.
    """
    req = Request(f"{PLUTO_API}/threads")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"Pluto API returned HTTP {resp.status} for /threads."
                )
    except HTTPError as e:
        raise RuntimeError(
            f"Pluto API rejected the token (HTTP {e.code} {e.reason}). "
            f"The Clerk session is alive but doesn't grant Pluto access."
        ) from e


def force_login() -> dict:
    """Programmatic re-auth for the server's 401-retry path.

    No browser flow in this mode — clear the in-memory cache and re-read
    cookies. If the user's Chrome session is gone the underlying
    RuntimeError propagates and the caller surfaces it."""
    global _token_cache, _agent_token_cache
    with _token_lock:
        _token_cache = None
        _agent_token_cache = None
        token = _fetch_token_unlocked()
    payload = _decode_jwt_payload(token)
    return {
        "access_token": token,
        "expires_at": int(payload["exp"])
        if isinstance(payload.get("exp"), (int, float))
        else 0,
        "email": payload.get("email", ""),
    }


def pluto_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_token()}"}


def agent_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_agent_token()}"}


# ── CLI subcommands ──────────────────────────────────────────────────────


def login() -> int:
    try:
        creds = force_login()
        _verify_pluto_api(creds["access_token"])
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    payload = _decode_jwt_payload(creds["access_token"])
    print(f"Logged in as {_label(payload)} (auth method: chrome).")
    return 0


def logout() -> int:
    global _token_cache
    with _token_lock:
        _token_cache = None
    print(
        f"Cleared in-process token cache. Sign out at {PLUTO_ORIGIN} to fully revoke."
    )
    return 0


def status() -> int:
    try:
        token = get_token()
        _verify_pluto_api(token)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    payload = _decode_jwt_payload(token)
    print(f"Logged in as {_label(payload)} (auth method: chrome).")
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        print(f"Token expires: {time.ctime(int(exp))}")
    return 0


def main(argv: list[str] | None = None) -> int:
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
