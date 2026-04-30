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

from .auth.auth import bearer_headers
from .clients import AgentClient, PlutoClient
from .config import get_settings
from .errors import MissingApiKeyError


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
    """Build typed Pluto + Agent clients with a cached API key.

    The key is resolved at startup if available and held in memory for
    normal requests (no per-request file I/O). If no key is configured at
    startup, the cache stays empty and ``headers_provider`` resolves on the
    first request — this lets the MCP server boot before the user has run
    ``/pluto-judge:login``, so the login slash command can recover the
    session without restarting Claude Code. On a 401 the client invokes
    ``auth_refresh``, which re-reads env/file once so a fresh login is
    picked up. If that retry also 401s, ``format_tool_error`` surfaces a
    login prompt to the model.

    On shutdown, drains any in-flight background optimize tasks so they
    don't run against a closed httpx client (which raises ``RuntimeError``
    on the event loop at process exit).
    """
    settings = get_settings()
    # Try to resolve at startup, but don't crash if the user hasn't logged in
    # yet — otherwise the MCP server fails to register tools and the user has
    # no way to recover via /pluto-judge:login without restarting Claude Code.
    try:
        initial_headers: dict[str, str] | None = bearer_headers()
    except MissingApiKeyError:
        initial_headers = None
    auth_state: dict[str, dict[str, str] | None] = {"headers": initial_headers}

    async def headers_provider() -> dict[str, str]:
        headers = auth_state["headers"]
        if headers is None:
            headers = bearer_headers()
            auth_state["headers"] = headers
        return headers

    async def auth_refresh() -> None:
        auth_state["headers"] = bearer_headers()

    async with AsyncExitStack() as stack:
        pluto = await stack.enter_async_context(
            PlutoClient(
                settings.pluto_client_config(),
                headers_provider=headers_provider,
                auth_refresh=auth_refresh,
            )
        )
        agent = await stack.enter_async_context(
            AgentClient(
                settings.agent_client_config(),
                headers_provider=headers_provider,
                auth_refresh=auth_refresh,
            )
        )
        state = ServerState(pluto=pluto, agent=agent)
        try:
            yield state
        finally:
            if state.background_tasks:
                await asyncio.gather(*state.background_tasks, return_exceptions=True)
