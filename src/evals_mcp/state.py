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
class OptimizeRun:
    """In-flight or terminal agent optimize run for a thread.

    ``task`` is a live :class:`asyncio.Task` from construction (never a
    placeholder) — consumers may call ``task.done()`` unconditionally.
    ``event`` is set the moment ``captured_id`` becomes non-None OR the
    background task terminates (success or failure). Resumers wait on it.

    Under the one-run-per-thread invariant enforced in
    :func:`_start_optimize_and_await_classifier`, an instance is the durable
    record of "this thread's optimize already happened" — captured_id (or
    captured_error) is what every subsequent call sees, never a fresh run.
    The sole exception: a run that terminates with ``captured_error`` set and
    ``captured_id`` still None (it died before a classifier surfaced) is
    dropped on the next explicit retry, since it protects no server-side
    state and the cause may be transient or fixable.
    """

    task: asyncio.Task[None]
    event: asyncio.Event
    captured_id: str | None = None
    captured_error: BaseException | None = None


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
    # One entry per thread_id that has ever started an optimize run in this
    # server's lifetime. Looked up first by _start_optimize_and_await_classifier
    # to enforce one-run-per-thread; a resumer arriving after the run finished
    # still sees the captured_id. The only entry ever removed is a run that
    # died before emitting a classifier_id (dropped on retry so a fresh run can
    # start); entries that emitted an id persist for the server's lifetime.
    optimize_runs: dict[str, OptimizeRun] = field(default_factory=lambda: dict[str, OptimizeRun]())


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
