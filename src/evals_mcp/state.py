"""Per-server lifespan state.

`ServerState` is a dataclass owned by the FastMCP lifespan context.
Tools reach it via `ctx.request_context.lifespan_context`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog
from mcp.server.fastmcp import FastMCP

from .auth.auth import BearerCache
from .clients import AgentClient, PlatformClient
from .config import get_settings

logger: Any = structlog.get_logger(__name__)


@dataclass
class ServerState:
    platform: PlatformClient
    agent: AgentClient
    has_questions: bool = False
    committed: bool = False
    # Whether the current evaluator's org may run SLM optimization. Set
    # in _send_message at commit time from `GET /plan`, then consumed by
    # _handle_optimize as the backstop that rejects `Optimize [SLM]` if
    # the orchestrator surfaces it despite the gated UX. Defaults to True
    # — the backstop is only meaningful after the post-commit plan check,
    # and an `Optimize [SLM]` arriving before commit isn't a real concern.
    slm_allowed: bool = True
    # Holds references to background optimize tasks so asyncio doesn't GC
    # them mid-flight. The set is cleaned up by the task's own done callback.
    background_tasks: set[asyncio.Task[None]] = field(default_factory=lambda: set())


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncGenerator[ServerState, None]:
    """Build typed Platform + Agent clients sharing one :class:`BearerCache`.

    The cache is mtime/inode-aware: a mid-session `auth login` (which
    rewrites the credentials file) is picked up on the very next request
    without a server restart, including the boot-before-login case.

    On shutdown, drains any in-flight background optimize tasks so they
    don't run against a closed httpx client.
    """
    settings = get_settings()
    bearer_cache = BearerCache()

    async with AsyncExitStack() as stack:
        platform = await stack.enter_async_context(
            PlatformClient(
                settings.platform_client_config(),
                headers_provider=bearer_cache.headers,
            )
        )
        agent = await stack.enter_async_context(
            AgentClient(
                base_url=settings.langgraph_url,
                assistant_id=settings.langgraph_assistant_id,
                timeout=settings.agent_http_timeout,
                headers_provider=bearer_cache.headers,
            )
        )
        state = ServerState(platform=platform, agent=agent)
        try:
            yield state
        finally:
            if state.background_tasks:
                # Cancel first, then drain with a bounded wait. Awaiting
                # without cancelling would block shutdown on the natural
                # completion of long-lived agent runs (~20 min for SLM
                # optimize), which lets the host force-kill us instead.
                for task in state.background_tasks:
                    task.cancel()
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(*state.background_tasks, return_exceptions=True),
                        timeout=5.0,
                    )
                except TimeoutError:
                    logger.warning(
                        "Background tasks did not finish cancelling within 5s; "
                        "leaving shutdown to proceed.",
                        task_count=len(state.background_tasks),
                    )
                else:
                    for r in results:
                        if isinstance(r, BaseException) and not isinstance(
                            r, asyncio.CancelledError
                        ):
                            logger.error(
                                "Background task raised on shutdown",
                                error=repr(r),
                            )
