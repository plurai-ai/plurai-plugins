"""Typed agent client.

Wraps the underlying agent SDK client. The SDK client is built lazily
on first use and reused across calls; the cached headers are compared
against ``headers_provider``'s output on each call so a credentials-file
update (typically a mid-session `auth login`) is picked up by rebuilding
the SDK client on the next call. The SDK takes headers as a
constructor-time argument, so rebuild is the only way to swap them.

Two public methods drive the rest of the codebase:

* :meth:`run_agent` streams a run end-to-end. The only stream events we
  inspect are ``on_custom_event`` frames carrying mid-run state
  snapshots (``classifier_id``, etc.) the optimize fast-path needs
  before the long-lived stream finishes. Other frames are ignored,
  except the ``error`` envelope which is re-raised as a ``RuntimeError``.
* :meth:`get_state` reads the checkpointed thread state via the SDK's
  ``threads.get_state``. Tools call this after ``run_agent`` returns
  to read final ``messages``, ``commit_id``, ``classifier_id``, etc.
  from the persistent state — no event-stream parsing required.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any, Self, cast

import httpx
import structlog
from langgraph_sdk import get_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .base import HeadersProvider

logger: Any = structlog.get_logger(__name__)

StateCallback = Callable[["ThreadStateView"], None]

_INTERMEDIATE_STATE_EVENT_NAME = "copilotkit_manually_emit_intermediate_state"

# The agent SDK serialises message instances with a ``type`` field
# ("human" / "ai" / "system" / "tool"); the OpenAI-style raw-dict form
# uses ``role`` ("user" / "assistant" / ...). Map the former to the
# latter so consumers only have to check one field.
_TYPE_TO_ROLE = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}


class AgentMessage(BaseModel):
    """One message in the agent's checkpointed ``messages`` list.

    Accepts both the agent SDK's serialized message shape
    (``{"type": "ai", "content": ...}``) and the OpenAI-style raw-dict
    form (``{"role": "assistant", "content": ...}``). After validation
    ``role`` is canonical (``user`` / ``assistant`` / ...). ``content``
    is coerced to ``str`` so callers don't need to special-case the
    multimodal ``list[ContentBlock]`` form the SDK occasionally emits.
    """

    model_config = ConfigDict(extra="ignore")

    role: str = ""
    content: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d: dict[str, Any] = dict(cast(dict[str, Any], data))
        if not d.get("role"):
            t = d.get("type")
            if isinstance(t, str):
                d["role"] = _TYPE_TO_ROLE.get(t, t)
        content = d.get("content")
        if content is not None and not isinstance(content, str):
            d["content"] = str(content)
        return d


class ThreadStateView(BaseModel):
    """Subset of the agent's checkpointed state we consume.

    ``extra='ignore'`` because the agent state has many fields beyond what
    tools read.
    """

    model_config = ConfigDict(extra="ignore")

    messages: list[AgentMessage] = Field(default_factory=lambda: [])
    classifier_id: str | None = None
    commit_id: str | None = None

    def last_assistant_message(self) -> str:
        """Latest assistant content as a string, or ``""`` if none.

        Skips the ``"..."`` placeholder the agent emits before tool calls.
        """
        for msg in reversed(self.messages):
            if msg.role == "assistant" and msg.content and msg.content != "...":
                return msg.content
        return ""


class AgentClient:
    """Async client for the Plurai agent endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        assistant_id: str,
        timeout: float,
        headers_provider: HeadersProvider,
    ) -> None:
        if not base_url:
            raise ValueError("AgentClient.base_url must be a non-empty string")
        if not assistant_id:
            raise ValueError("AgentClient.assistant_id must be a non-empty string")
        if timeout <= 0:
            raise ValueError(f"AgentClient.timeout must be > 0 (got {timeout})")
        self._base_url = base_url
        self._assistant_id = assistant_id
        self._timeout = timeout
        self._headers_provider = headers_provider
        self._client: Any | None = None
        self._cached_headers: Mapping[str, str] | None = None
        self._client_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self._aclose_cached_client()
        self._cached_headers = None

    async def _aclose_cached_client(self) -> None:
        """Close the cached SDK client and drop the reference.

        The SDK client owns an ``httpx.AsyncClient`` that holds a
        connection pool and per-instance ``AsyncHTTPTransport`` with
        retries. Without ``aclose()`` those resources leak — this
        matters most on header rotation (every mid-session `auth login`)
        and lifespan shutdown.
        """
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.aclose()
        except Exception:
            logger.exception("Failed to close cached agent SDK client", base_url=self._base_url)

    async def run_agent(
        self,
        thread_id: str,
        message: str,
        *,
        on_state: StateCallback | None = None,
    ) -> None:
        """Stream an agent run; resolve when the run stream closes.

        ``on_state`` (optional) fires synchronously each time the agent
        emits a mid-run state snapshot via a custom event. The optimize
        fast-path uses this to grab ``classifier_id`` mid-stream while
        the run is still going in the background. For final state,
        callers should invoke :meth:`get_state` after this returns.

        ``self._timeout`` is the configured ``agent_http_timeout``
        (default 300s). The agent injects synthetic events while idle,
        so the client never sees a legitimate gap on a healthy stream —
        the timeout gives substantial margin for jitter while still
        surfacing a truly dead socket within a bounded window.
        """
        await self._stream(thread_id, message, on_state)

    async def get_state(self, thread_id: str) -> ThreadStateView:
        """Read the checkpointed thread state for ``thread_id``.

        Wraps the SDK's ``threads.get_state`` and projects ``values``
        into :class:`ThreadStateView`. An empty view is returned when no
        checkpoint exists yet (e.g. before the first run).
        """
        return await self._fetch_state(thread_id)

    async def _get_client(self) -> Any:
        async with self._client_lock:
            headers = self._headers_provider()
            if self._client is not None and self._cached_headers == headers:
                return self._client
            await self._aclose_cached_client()
            self._client = get_client(
                url=self._base_url,
                headers=headers,
                timeout=httpx.Timeout(self._timeout),
            )
            self._cached_headers = headers
            return self._client

    async def _stream(
        self,
        thread_id: str,
        message: str,
        on_state: StateCallback | None,
    ) -> None:
        client = await self._get_client()
        async for part in client.runs.stream(
            thread_id=thread_id,
            assistant_id=self._assistant_id,
            input={"messages": [{"role": "user", "content": message}]},
            stream_mode="events",
        ):
            envelope: str = part.event
            payload: Any = part.data
            if envelope == "error":
                raise RuntimeError(f"Agent stream error: {payload!r}")
            if on_state is None or envelope != "events" or not isinstance(payload, dict):
                continue
            inner = cast(dict[str, Any], payload)
            if (
                inner.get("event") != "on_custom_event"
                or inner.get("name") != _INTERMEDIATE_STATE_EVENT_NAME
            ):
                continue
            logger.debug("Received intermediate state event", thread_id=thread_id, payload=payload)
            try:
                snapshot = ThreadStateView.model_validate(inner.get("data"))
            except ValidationError:
                # Schema drift: log loudly with the offending payload so a
                # silent stream of validation failures surfaces in logs
                # rather than presenting as a stalled optimize fast-path.
                logger.exception(
                    "Failed to parse intermediate state event",
                    thread_id=thread_id,
                    data=inner.get("data"),
                )
                continue
            try:
                on_state(snapshot)
            except Exception:
                logger.exception("on_state callback raised", thread_id=thread_id)

    async def _fetch_state(self, thread_id: str) -> ThreadStateView:
        client = await self._get_client()
        snapshot: Any = await client.threads.get_state(thread_id)
        # ``threads.get_state`` returns the SDK's ``ThreadState`` TypedDict;
        # ``values`` is the checkpointed graph state dict (or absent when
        # no checkpoint exists yet).
        if not isinstance(snapshot, dict):
            raise RuntimeError(f"Unexpected get_state response shape: {type(snapshot).__name__}")
        values = cast(dict[str, Any], snapshot).get("values")
        if values is None:
            # Legitimate "no checkpoint yet" — e.g. first call before any run.
            return ThreadStateView()
        if not isinstance(values, dict):
            raise RuntimeError(f"Unexpected get_state values shape: {type(values).__name__}")
        return ThreadStateView.model_validate(values)
