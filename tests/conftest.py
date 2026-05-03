"""Shared test fixtures.

Tests build a `ServerState` with real :class:`PlatformClient` and
:class:`AgentClient` instances; their underlying ``httpx.AsyncClient`` is
intercepted by ``pytest_httpx``. Auth is stubbed so tests never hit a
live auth backend.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from evals_mcp.clients import AgentClient, BaseHttpClientConfig, PlatformClient
from evals_mcp.config import get_settings
from evals_mcp.state import ServerState


async def _fake_platform_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


async def _fake_agent_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


@pytest.fixture
def fake_force_login() -> AsyncMock:
    """Refresh-on-401 hook as an AsyncMock so tests can assert call count."""
    return AsyncMock(return_value=None)


@pytest_asyncio.fixture
async def state(httpx_mock: Any, fake_force_login: AsyncMock) -> AsyncIterator[ServerState]:
    """ServerState with PlatformClient + AgentClient routed through pytest-httpx."""
    _ = httpx_mock  # activate the fixture so requests are intercepted
    settings = get_settings()
    platform_config = BaseHttpClientConfig(api_url=settings.platform_api, max_retries=0)
    agent_config = BaseHttpClientConfig(api_url=settings.agent_api_base, max_retries=0)
    async with (
        PlatformClient(
            platform_config,
            headers_provider=_fake_platform_headers,
            auth_refresh=fake_force_login,
        ) as platform,
        AgentClient(
            agent_config,
            headers_provider=_fake_agent_headers,
            auth_refresh=fake_force_login,
        ) as agent,
    ):
        yield ServerState(platform=platform, agent=agent)


class FakeRequestContext:
    def __init__(self, lifespan_context: ServerState) -> None:
        self.lifespan_context = lifespan_context


class FakeContext:
    """Minimal stand-in for FastMCP's Context — just enough surface for our tools."""

    def __init__(self, state: ServerState, *, elicit_action: str = "decline") -> None:
        self.request_context = FakeRequestContext(state)
        self._elicit_action = elicit_action

    async def elicit(self, *, message: str, schema: type) -> Any:
        # Default: decline, exercising the AskUserQuestion fallback path.
        class _Result:
            def __init__(self, action: str) -> None:
                self.action = action
                self.data = None

        return _Result(self._elicit_action)


@pytest.fixture
def ctx(state: ServerState) -> FakeContext:
    return FakeContext(state)
