"""User-API-key auth for the evals MCP server.

The Plurai REST API and CopilotKit agent endpoints both accept the user's
long-lived API key as ``Authorization: Bearer ak…``. This module:

- resolves the key from a JSON file at ``~/.config/evals/credentials.json``,
- exposes ``platform_headers`` / ``agent_headers`` returning the bearer
  header (raises :class:`~evals_mcp.errors.MissingApiKeyError` when no
  key is configured, :class:`~evals_mcp.errors.CorruptCredentialsError`
  when the file exists but is broken),
- provides a tiny CLI (``login --key``, ``logout``, ``status``) used by
  the ``/login`` slash command.

Self-contained — usable standalone for testing:

    python -m evals_mcp auth login --key ak_test_xyz
    python -m evals_mcp auth status
    python -m evals_mcp auth logout
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from ..config import get_settings
from ..errors import CorruptCredentialsError, MissingApiKeyError


def _credentials_path() -> Path:
    return Path(get_settings().credentials_path).expanduser()


def load_api_key() -> str | None:
    """Return the API key from the credentials file, or ``None`` if absent.

    Raises :class:`CorruptCredentialsError` when a file exists but cannot be
    decoded — distinguishing "not logged in" from "credentials broken" so
    the user isn't stuck in a misleading login loop.
    """
    path = _credentials_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise CorruptCredentialsError(path, str(e)) from e
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as e:
        raise CorruptCredentialsError(path, f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise CorruptCredentialsError(path, "JSON root is not an object")
    key = cast(dict[str, Any], data).get("api_key")
    if key is None:
        return None
    if not isinstance(key, str):
        raise CorruptCredentialsError(path, "'api_key' is not a string")
    stripped = key.strip()
    return stripped or None


def save_api_key(key: str) -> Path:
    """Persist the API key to the credentials file (file 0600, dir 0700).

    Writes via tmp + ``os.replace`` so a crash can't leave a half-written
    file in place that subsequent reads would treat as corrupt.
    """
    if not key or not key.strip():
        raise ValueError("API key must be a non-empty string.")
    path = _credentials_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdir's mode argument is a no-op when the dir already exists;
        # explicitly chmod the leaf so credentials never live in a
        # world-readable directory because of an earlier umask.
        os.chmod(path.parent, 0o700)
        tmp = path.with_suffix(path.suffix + ".tmp")
        # os.open with explicit 0o600 prevents the umask from widening
        # permissions that path.write_text() would honor by default.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"api_key": key.strip()}, f)
        os.replace(tmp, path)
    except OSError as e:
        raise OSError(f"Failed to persist credentials to {path}: {e}") from e
    return path


def delete_api_key() -> bool:
    """Remove the credentials file. Returns True if it existed.

    Tolerates a TOCTOU race where the file is removed between the existence
    check and the unlink call.
    """
    path = _credentials_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        raise OSError(f"Failed to remove credentials at {path}: {e}") from e
    return True


def bearer_headers() -> dict[str, str]:
    key = load_api_key()
    if not key:
        raise MissingApiKeyError()
    return {"Authorization": f"Bearer {key}"}


# Kept as separate seams for the platform REST API vs the agent endpoint so
# callers can use the role-appropriate function — they share a key today but
# may diverge if the agent surface ever requires different scopes/headers.
def platform_headers() -> dict[str, str]:
    return bearer_headers()


def agent_headers() -> dict[str, str]:
    return bearer_headers()


# ── CLI ──────────────────────────────────────────────────────────────────


def _cmd_login(key: str) -> int:
    try:
        path = save_api_key(key)
    except (OSError, ValueError) as e:
        print(f"Failed to save API key: {e}", file=sys.stderr)
        return 1
    print(f"Saved API key to {path}.")
    return 0


def _cmd_logout() -> int:
    try:
        removed = delete_api_key()
    except OSError as e:
        print(f"Failed to remove API key: {e}", file=sys.stderr)
        return 1
    if removed:
        print(f"Removed {_credentials_path()}.")
    else:
        print("No saved API key.")
    return 0


def _cmd_status() -> int:
    try:
        key = load_api_key()
    except CorruptCredentialsError as e:
        print(str(e), file=sys.stderr)
        return 1
    if key:
        print("API key configured.")
        return 0
    print("No API key configured.")
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. argv is the list of args after the `auth` keyword."""
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="evals-mcp auth")
    sub = parser.add_subparsers(dest="cmd", required=True)
    login = sub.add_parser("login", help="Save the Plurai API key.")
    login.add_argument("--key", required=True, help="API key (e.g. ak_…).")
    sub.add_parser("logout", help="Remove the saved API key.")
    sub.add_parser("status", help="Show whether an API key is configured.")
    args = parser.parse_args(argv)

    if args.cmd == "login":
        return _cmd_login(cast(str, args.key))
    if args.cmd == "logout":
        return _cmd_logout()
    return _cmd_status()
