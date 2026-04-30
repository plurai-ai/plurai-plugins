"""User-API-key auth for pluto-judge.

The Pluto API and CopilotKit agent endpoints both accept the user's
long-lived API key as ``Authorization: Bearer ak…``. This module:

- resolves the key (env var ``PLUTO_API_KEY`` first, then a JSON file at
  ``~/.config/pluto/credentials.json`` — overridable via
  ``PLUTO_CREDENTIALS_PATH``),
- exposes ``pluto_headers`` / ``agent_headers`` returning the bearer
  header (raises :class:`~pluto_judge.errors.MissingApiKeyError` when no
  key is configured),
- provides a tiny CLI (``login --key``, ``logout``, ``status``) used by
  the ``/pluto-judge:login`` slash command.

Self-contained — usable standalone for testing:

    python -m pluto_judge auth login --key ak_test_xyz
    python -m pluto_judge auth status
    python -m pluto_judge auth logout
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from ..errors import MissingApiKeyError

_ENV_VAR = "PLUTO_API_KEY"
_DEFAULT_CREDS_PATH = "~/.config/pluto/credentials.json"


def _credentials_path() -> Path:
    return Path(os.environ.get("PLUTO_CREDENTIALS_PATH", _DEFAULT_CREDS_PATH)).expanduser()


def _read_file_key() -> str | None:
    path = _credentials_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"WARNING: Could not load credentials at {path} ({e}); treating as not logged in.",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, dict):
        return None
    key = cast(dict[str, Any], data).get("api_key")
    return key if isinstance(key, str) and key else None


def load_api_key() -> str | None:
    """Resolve the API key. Env var wins over the credentials file."""
    env_key = os.environ.get(_ENV_VAR, "").strip()
    if env_key:
        return env_key
    return _read_file_key()


def save_api_key(key: str) -> Path:
    """Persist the API key to the credentials file (0600, dir 0700)."""
    if not key or not key.strip():
        raise ValueError("API key must be a non-empty string.")
    path = _credentials_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"api_key": key.strip()}, f)
    return path


def delete_api_key() -> bool:
    """Remove the credentials file. Returns True if it existed."""
    path = _credentials_path()
    existed = path.exists()
    if existed:
        path.unlink()
    return existed


def bearer_headers() -> dict[str, str]:
    key = load_api_key()
    if not key:
        raise MissingApiKeyError()
    return {"Authorization": f"Bearer {key}"}


def pluto_headers() -> dict[str, str]:
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
    if delete_api_key():
        print(f"Removed {_credentials_path()}.")
        return 0
    print("No saved API key.")
    return 0


def _cmd_status() -> int:
    key = load_api_key()
    if key:
        print("API key configured.")
        return 0
    print("No API key configured.")
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. argv is the list of args after the `auth` keyword."""
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="pluto_judge auth")
    sub = parser.add_subparsers(dest="cmd")
    login = sub.add_parser("login", help="Save a Pluto API key.")
    login.add_argument("--key", required=True, help="Pluto API key (e.g. ak_…).")
    sub.add_parser("logout", help="Remove the saved API key.")
    sub.add_parser("status", help="Show whether an API key is configured.")
    args = parser.parse_args(argv)

    if args.cmd == "login":
        return _cmd_login(args.key)
    if args.cmd == "logout":
        return _cmd_logout()
    # Default to status when no subcommand is given.
    return _cmd_status()


if __name__ == "__main__":
    sys.exit(main())
