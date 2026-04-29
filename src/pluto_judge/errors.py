"""Safe formatting of backend error bodies for forwarding into model context.

Caps length, redacts secret-shaped JSON values. Ported from the urllib-based
helper but operates on `httpx.Response` instead of `urllib.error.HTTPError`.
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx

_ERROR_BODY_MAX_BYTES = 2000
_ERROR_REDACT_KEYS: tuple[str, ...] = (
    "authorization",
    "token",
    "access_token",
    "secret",
    "api_key",
)


def safe_error_body(exc: httpx.HTTPStatusError) -> str:
    """Read an HTTP error body for client display: truncate and redact secrets."""
    try:
        raw: bytes = exc.response.content
    except Exception:
        return str(exc)
    body = raw[:_ERROR_BODY_MAX_BYTES].decode("utf-8", errors="replace")
    truncated = len(raw) > _ERROR_BODY_MAX_BYTES
    try:
        parsed: Any = json.loads(body)
    except json.JSONDecodeError:
        return body + ("…[truncated]" if truncated else "")
    _redact(parsed)
    return json.dumps(parsed) + ("…[truncated]" if truncated else "")


def _redact(node: Any) -> None:
    if isinstance(node, dict):
        node_dict = cast("dict[Any, Any]", node)
        for k in list(node_dict.keys()):
            if isinstance(k, str) and k.lower() in _ERROR_REDACT_KEYS:
                node_dict[k] = "[redacted]"
            else:
                _redact(node_dict[k])
    elif isinstance(node, list):
        node_list = cast("list[Any]", node)
        for item in node_list:
            _redact(item)
