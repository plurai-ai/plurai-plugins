"""User-API-key auth for the evals MCP server.

Both the Plurai REST API and the agent endpoint accept the user's
long-lived API key as ``Authorization: Bearer ak…``. This module:

- resolves the key from a JSON file at ``~/.config/evals/credentials.json``,
- exposes :func:`bearer_headers` returning the ``Authorization: Bearer``
  header (raises :class:`~evals_mcp.errors.MissingApiKeyError` when no
  key is configured, :class:`~evals_mcp.errors.CorruptCredentialsError`
  when the file exists but is broken). The result is memoised against
  the file's (inode, mtime_ns), so a fresh `auth login` (which rewrites
  the file) is picked up on the next call without explicit invalidation.
- provides a tiny CLI (``login --key``, ``logout``, ``status``) invoked
  inline by the model when an evals tool reports missing/invalid creds.

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

import structlog

from ..config import get_settings
from ..errors import CorruptCredentialsError, MissingApiKeyError

logger: Any = structlog.get_logger(__name__)


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


class BearerCache:
    """Mtime/inode-aware cache for the ``Authorization: Bearer`` header.

    `auth login` rewrites the credentials file, which bumps both st_ino
    and st_mtime_ns; the next call to :meth:`headers` notices the
    mismatch and re-reads. The path is part of the cache key so a
    settings change (e.g. tests redirecting ``HOME``) doesn't see stale
    entries.

    Instantiate once per server lifespan and pass :meth:`headers` as the
    ``headers_provider`` to HTTP clients.
    """

    def __init__(self) -> None:
        self._path: Path | None = None
        self._stat_key: tuple[int, int] | None = None
        self._headers: dict[str, str] | None = None

    def headers(self) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <key>"}``, re-reading the file
        only when its (inode, mtime_ns) has changed. Raises
        :class:`MissingApiKeyError` when no key is configured.

        Not thread-safe — single asyncio event loop only. The three-field
        update at the bottom is a single critical section under that model.
        """
        path = _credentials_path()
        try:
            st = path.stat()
            stat_key: tuple[int, int] | None = (st.st_ino, st.st_mtime_ns)
        except FileNotFoundError:
            stat_key = None
        except OSError as e:
            # Permission denied, NFS hiccup, symlink loop, etc. Surface as
            # CorruptCredentialsError so the user gets the actionable
            # "credentials file is broken — re-run auth login" message
            # instead of a generic ``Unexpected OSError`` envelope.
            raise CorruptCredentialsError(path, str(e)) from e
        if path == self._path and stat_key == self._stat_key and self._headers is not None:
            return self._headers
        key = load_api_key()
        if not key:
            raise MissingApiKeyError()
        logger.info("BearerCache refreshed from disk")
        # Assign cache only after the read succeeds so a partial failure
        # leaves the previous (still-valid) cache untouched.
        self._headers = {"Authorization": f"Bearer {key}"}
        self._path = path
        self._stat_key = stat_key
        return self._headers


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
