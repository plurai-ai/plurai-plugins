"""Shared test fixtures.

Tests build a `ServerState` with real :class:`PlatformClient` and
:class:`AgentClient` instances. The platform client's ``httpx.AsyncClient``
is intercepted by ``pytest_httpx``; the agent client's underlying SDK is
replaced with an in-memory fake (see :class:`FakeLangGraphClient`). Auth
is stubbed so tests never hit a live auth backend.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from evals_mcp.clients import AgentClient, BaseHttpClientConfig, PlatformClient
from evals_mcp.config import get_settings
from evals_mcp.state import ServerState


def _fake_bearer_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


class FakeStreamPart:
    """Mirror of ``langgraph_sdk.schema.StreamPart`` for the fake client."""

    __slots__ = ("data", "event")

    def __init__(self, event: str, data: Any) -> None:
        self.event = event
        self.data = data


class _FakeRuns:
    def __init__(self) -> None:
        # Each entry is a list of frames yielded for one ``stream`` call.
        # If the test only configures a single list, it's reused for every
        # call (most tests only invoke run_agent once).
        self.frames_by_call: list[list[FakeStreamPart]] = []
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> AsyncIterator[FakeStreamPart]:
        self.calls.append(kwargs)
        if not self.frames_by_call:
            frames: list[FakeStreamPart] = []
        elif len(self.frames_by_call) == 1:
            frames = self.frames_by_call[0]
        else:
            frames = self.frames_by_call.pop(0)

        async def _iter() -> AsyncIterator[FakeStreamPart]:
            for f in frames:
                yield f

        return _iter()


class _FakeThreads:
    """Backs ``client.threads.get_state``.

    Tests set ``state_values`` to the ``values`` dict the server would
    return for a checkpointed thread state. Defaults to an empty dict so
    ``ThreadStateView`` validates as an empty view (no messages, no IDs).
    """

    def __init__(self) -> None:
        self.state_values: dict[str, Any] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_state(self, thread_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((thread_id, kwargs))
        return {"values": dict(self.state_values), "next": []}


class FakeLangGraphClient:
    """In-memory stand-in for ``langgraph_sdk.client.LangGraphClient``.

    Tests configure ``runs.frames_by_call`` (or use the convenience
    ``set_frames`` method) to script the stream output for upcoming
    ``run_agent`` calls, and ``set_state`` to script the ``threads.get_state``
    response that ``AgentClient.get_state`` returns.
    """

    def __init__(self) -> None:
        self.runs = _FakeRuns()
        self.threads = _FakeThreads()
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.construction_calls: list[dict[str, Any]] = []
        self.aclose_calls: int = 0

    def set_frames(self, frames: list[FakeStreamPart]) -> None:
        self.runs.frames_by_call = [frames]

    def set_state(self, values: dict[str, Any]) -> None:
        self.threads.state_values = values

    async def aclose(self) -> None:
        self.aclose_calls += 1


@pytest.fixture
def langgraph_client() -> FakeLangGraphClient:
    return FakeLangGraphClient()


@pytest_asyncio.fixture
async def state(
    httpx_mock: Any,
    langgraph_client: FakeLangGraphClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ServerState]:
    """ServerState with PlatformClient on httpx_mock and AgentClient on the fake."""
    _ = httpx_mock  # activate the fixture so platform requests are intercepted
    settings = get_settings()
    platform_config = BaseHttpClientConfig(api_url=settings.platform_api, max_retries=0)

    def _fake_get_client(*, url: str, headers: Any = None, timeout: Any = None) -> Any:
        del timeout
        recorded = dict(headers or {})
        langgraph_client.last_url = url
        langgraph_client.last_headers = recorded
        langgraph_client.construction_calls.append({"url": url, "headers": recorded})
        return langgraph_client

    monkeypatch.setattr("evals_mcp.clients.agent.get_client", _fake_get_client)

    async with (
        PlatformClient(
            platform_config,
            headers_provider=_fake_bearer_headers,
        ) as platform,
        AgentClient(
            base_url=settings.langgraph_url,
            assistant_id=settings.langgraph_assistant_id,
            timeout=settings.agent_http_timeout,
            headers_provider=_fake_bearer_headers,
        ) as agent,
    ):
        yield ServerState(platform=platform, agent=agent)


class FakeRequestContext:
    def __init__(self, lifespan_context: ServerState) -> None:
        self.lifespan_context = lifespan_context


class FakeContext:
    """Minimal stand-in for FastMCP's Context — just enough surface for our tools."""

    def __init__(self, state: ServerState) -> None:
        self.request_context = FakeRequestContext(state)


@pytest.fixture
def ctx(state: ServerState) -> FakeContext:
    return FakeContext(state)
