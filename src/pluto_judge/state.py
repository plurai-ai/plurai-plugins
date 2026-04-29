"""Per-server lifespan state.

`ServerState` is a dataclass owned by the FastMCP lifespan context.
Tools reach it via `ctx.request_context.lifespan_context`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

from mcp.server.fastmcp import FastMCP

from .auth import agent_headers, force_login, pluto_headers
from .clients import AgentClient, PlutoClient
from .config import get_settings


async def _async_pluto_headers() -> dict[str, str]:
    return dict(await asyncio.to_thread(pluto_headers))


async def _async_agent_headers() -> dict[str, str]:
    return dict(await asyncio.to_thread(agent_headers))


async def _async_force_login() -> None:
    await asyncio.to_thread(force_login)


@dataclass
class ServerState:
    pluto: PlutoClient
    agent: AgentClient
    classifier_by_thread: dict[str, str] = field(default_factory=lambda: {})
    has_questions: bool = False
    # Holds references to background optimize tasks so asyncio doesn't GC
    # them mid-flight. The set is cleaned up by the task's own done callback.
    background_tasks: set[asyncio.Task[None]] = field(default_factory=lambda: set())


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncGenerator[ServerState, None]:
    """Build typed Pluto + Agent clients with shared dynamic auth.

    On shutdown, drains any in-flight background optimize tasks so they
    don't run against a closed httpx client (which raises ``RuntimeError``
    on the event loop at process exit).
    """
    settings = get_settings()
    async with AsyncExitStack() as stack:
        pluto = await stack.enter_async_context(
            PlutoClient(
                settings.pluto_client_config(),
                headers_provider=_async_pluto_headers,
                auth_refresh=_async_force_login,
            )
        )
        agent = await stack.enter_async_context(
            AgentClient(
                settings.agent_client_config(),
                headers_provider=_async_agent_headers,
                auth_refresh=_async_force_login,
            )
        )
        state = ServerState(pluto=pluto, agent=agent)
        try:
            yield state
        finally:
            if state.background_tasks:
                await asyncio.gather(*state.background_tasks, return_exceptions=True)
