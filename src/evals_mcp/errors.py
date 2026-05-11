"""Safe formatting of backend error bodies for forwarding into model context.

Caps length, redacts secret-shaped JSON values. Operates on
`httpx.Response`. Also exposes `format_tool_error` for tool wrappers, which
turns transport / status / auth errors into a consistent
``{"error": ...}`` envelope rather than letting the FastMCP runtime turn
them into opaque protocol errors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx

_LOGIN_PROMPT = (
    "Ask the user to paste their Plurai API key, save it via "
    "`uv run --project ${CLAUDE_PLUGIN_ROOT} python -m evals_mcp auth login --key <KEY>`, "
    "then retry the failed tool call."
)

_REVOKED_KEY_PROMPT = (
    "The key on disk was rejected by the server — it is revoked, expired, or otherwise invalid. "
    "If you have a Plurai API key from earlier in this conversation, that IS the rejected key — "
    "do NOT call `auth login` with it. You MUST ask the user (in chat, this turn) to paste a "
    "freshly-generated key from https://app.plurai.ai/settings?tab=api-keys (Create new key); "
    "warn them the key will appear in this conversation. Only after the user supplies a new key "
    "in this turn, run "
    "`uv run --project ${CLAUDE_PLUGIN_ROOT} python -m evals_mcp auth login --key <NEW_KEY>` "
    "and retry the failed tool call."
)


class MissingApiKeyError(RuntimeError):
    """Raised when no Plurai API key is configured.

    Surfaced to tool callers so the model runs the inline auth flow
    instead of bouncing the prompt back to the user.
    """

    def __init__(self) -> None:
        super().__init__(f"Plurai API key not set. {_LOGIN_PROMPT}")


class CorruptCredentialsError(RuntimeError):
    """Raised when the credentials file exists but cannot be loaded.

    Distinguished from :class:`MissingApiKeyError` so the user gets an
    actionable message naming the broken file rather than the generic
    "not logged in" prompt that masks the real cause.
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        super().__init__(f"Credentials file at {path} is unreadable: {reason}. {_LOGIN_PROMPT}")


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
    except (httpx.ResponseNotRead, httpx.StreamConsumed, RuntimeError):
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


def format_tool_error(exc: BaseException) -> dict[str, str]:
    """Map any tool exception to a `{"error": ...}` envelope.

    Recognized classes get tailored messages (auth, HTTP status, transport,
    runtime). Anything else falls through to a generic envelope so tools
    never propagate raw exceptions to the FastMCP runtime — that would
    surface as an opaque protocol error with no actionable detail for the
    model.
    """
    if isinstance(exc, MissingApiKeyError):
        return {"error": str(exc)}
    if isinstance(exc, CorruptCredentialsError):
        return {"error": str(exc)}
    if isinstance(exc, httpx.HTTPStatusError):
        # langgraph_sdk's APIConnectionError / APITimeoutError extend
        # HTTPStatusError but pass response=None — treat them as transport
        # errors rather than dereferencing the missing response.
        response = cast("httpx.Response | None", exc.response)
        if response is None:
            return {"error": f"Network error reaching Plurai: {exc}"}
        if response.status_code == 401:
            return {"error": f"Plurai API key invalid or expired. {_REVOKED_KEY_PROMPT}"}
        return {"error": f"HTTP {response.status_code}: {safe_error_body(exc)}"}
    if isinstance(exc, httpx.TransportError):
        return {"error": f"Network error reaching Plurai: {exc}"}
    if isinstance(exc, RuntimeError):
        return {"error": f"Plurai request failed: {exc}"}
    return {"error": f"Unexpected {type(exc).__name__}: {exc}"}
