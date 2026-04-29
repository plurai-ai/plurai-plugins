"""Per-server lifespan state.

Replaces the module globals from the legacy server (`_agent_has_questions`,
`_classifier_by_thread`) with a `ServerState` dataclass owned by the FastMCP
lifespan context. Tools reach state via `ctx.request_context.lifespan_context`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import httpx

from . import auth as _auth
from .http import ForceLoginFn, HeadersFn, PlutoClient

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

# The auth subpackage is excluded from strict pyright (legacy urllib code).
# Cast at the boundary so the rest of the package stays strict-clean —
# pyright partially infers from the backend modules but not enough to satisfy
# strict mode without help.
_pluto_headers: HeadersFn = _auth.pluto_headers
_agent_headers: HeadersFn = _auth.agent_headers
_force_login: ForceLoginFn = cast(
    ForceLoginFn,
    _auth.force_login,  # pyright: ignore[reportUnknownMemberType]
)


@dataclass
class ServerState:
    pluto: PlutoClient
    agent: PlutoClient
    classifier_by_thread: dict[str, str] = field(default_factory=lambda: {})
    has_questions: bool = False
    # Holds references to background optimize tasks so asyncio doesn't GC
    # them mid-flight. The set is cleaned up by the task's own done callback.
    background_tasks: set[asyncio.Task[None]] = field(default_factory=lambda: set())


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncGenerator[ServerState, None]:
    """Create one shared httpx client and bind two PlutoClient views over it."""
    async with httpx.AsyncClient() as client:
        yield ServerState(
            pluto=PlutoClient(client, _pluto_headers, _force_login),
            agent=PlutoClient(client, _agent_headers, _force_login),
        )
