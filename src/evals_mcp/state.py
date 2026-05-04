"""Per-server lifespan state.

`ServerState` is a dataclass owned by the FastMCP lifespan context.
Tools reach it via `ctx.request_context.lifespan_context`.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

from mcp.server.fastmcp import FastMCP

from .auth.auth import bearer_headers
from .clients import AgentClient, PlatformClient
from .config import get_settings
from .errors import CorruptCredentialsError, MissingApiKeyError


@dataclass
class ServerState:
    platform: PlatformClient
    agent: AgentClient
    has_questions: bool = False
    # Mirror of the agent's STATE_SNAPSHOT.commit_id. Non-null means the
    # initial flow (synthetic data generation) is done — the agent has
    # committed an example set. Drives URL surfacing in _send_message and
    # the SLM/LLM format hint in the ask_user fallback path. Not used as
    # a gate.
    commit_id: str | None = None
    # Holds references to background optimize tasks so asyncio doesn't GC
    # them mid-flight. The set is cleaned up by the task's own done callback.
    background_tasks: set[asyncio.Task[None]] = field(default_factory=lambda: set())


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncGenerator[ServerState, None]:
    """Build typed Platform + Agent clients backed by a cached API key.

    The key is resolved at startup if available and held in memory for
    normal requests (no per-request file I/O). If no key is configured at
    startup, the cache stays empty and the first request resolves it
    lazily — this lets the MCP server boot before the user has run
    ``/login``, so the login slash command can recover the session
    without restarting Claude Code.

    On shutdown, drains any in-flight background optimize tasks so they
    don't run against a closed httpx client.
    """
    settings = get_settings()
    cached_headers: dict[str, str] | None = None
    try:
        cached_headers = bearer_headers()
    except MissingApiKeyError:
        print(
            "evals: no API key at startup; tools will require /login.",
            file=sys.stderr,
        )
    except CorruptCredentialsError as e:
        print(f"evals: {e}", file=sys.stderr)

    async def headers_provider() -> dict[str, str]:
        nonlocal cached_headers
        if cached_headers is None:
            cached_headers = bearer_headers()
        return cached_headers

    async def auth_refresh() -> None:
        nonlocal cached_headers
        # Clear first so a failed refresh leaves the cache empty rather
        # than continuing to serve previously-valid (now stale) headers.
        cached_headers = None
        cached_headers = bearer_headers()

    async with AsyncExitStack() as stack:
        platform = await stack.enter_async_context(
            PlatformClient(
                settings.platform_client_config(),
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
        state = ServerState(platform=platform, agent=agent)
        try:
            yield state
        finally:
            if state.background_tasks:
                # Cancel first, then drain with a bounded wait. Awaiting
                # without cancelling would block shutdown on the natural
                # completion of long-lived SSE streams (~20 min for SLM
                # optimize), which lets the host force-kill us instead.
                for task in state.background_tasks:
                    task.cancel()
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(*state.background_tasks, return_exceptions=True),
                        timeout=5.0,
                    )
                except TimeoutError:
                    print(
                        "evals: background tasks did not finish cancelling within 5s; "
                        "leaving shutdown to proceed.",
                        file=sys.stderr,
                    )
                else:
                    for r in results:
                        if isinstance(r, BaseException) and not isinstance(
                            r, asyncio.CancelledError
                        ):
                            print(
                                f"evals: background task raised on shutdown: {r!r}",
                                file=sys.stderr,
                            )
