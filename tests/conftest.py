"""Shared test fixtures.

Tests build a `ServerState` with a real `httpx.AsyncClient` whose transport
is a `pytest_httpx` mock — no network. Auth is stubbed to a static header.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from pluto_judge.http import PlutoClient
from pluto_judge.state import ServerState


def _fake_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _fake_force_login() -> dict[str, Any]:
    return {"access_token": "test-token", "expires_at": 9999999999}


@pytest_asyncio.fixture
async def state(httpx_mock: Any) -> AsyncIterator[ServerState]:
    """ServerState bound to a real httpx.AsyncClient routed through pytest-httpx."""
    _ = httpx_mock  # activate the fixture so requests are intercepted
    async with httpx.AsyncClient() as client:
        yield ServerState(
            pluto=PlutoClient(client, _fake_headers, _fake_force_login),
            agent=PlutoClient(client, _fake_headers, _fake_force_login),
        )


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
