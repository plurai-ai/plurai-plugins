"""Typed CopilotKit agent client.

The Plurai agent endpoint at ``/api/agent/api/copilotkit`` accepts a
single envelope shape and replies with a Server-Sent Events stream of
``MESSAGES_SNAPSHOT`` / ``STATE_SNAPSHOT`` / etc. events. Tools call
:meth:`AgentClient.run_agent`, get back a list of typed events, and
walk them with helpers in ``tools/evaluator.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from .base import BaseHttpClient
from .models import AgentEnvelope, AgentEvent, AgentMessage, AgentRunBody

# Path of the agent run endpoint, relative to the configured base_url.
# Public so tests can assert against the resolved URL without re-hardcoding
# the literal.
RUN_PATH = "/copilotkit"


class AgentClient(BaseHttpClient):
    """Async client for the Plurai agent (CopilotKit) endpoint."""

    _client_label = "Plurai Agent"

    async def run_agent(
        self,
        thread_id: str,
        message: str,
        *,
        run_id: str | None = None,
        timeout: float | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[AgentEvent]:
        """Execute an agent run; return all SSE events once the stream closes.

        ``on_event`` (optional) is fired synchronously for each decoded event
        as it arrives — used by callers (e.g. the optimize fast-path) that
        need to extract state mid-stream while the agent run is still
        ongoing in the background.
        """
        envelope = AgentEnvelope(
            method="agent/run",
            params={"agentId": "agent"},
            body=AgentRunBody(
                thread_id=thread_id,
                run_id=run_id or str(uuid.uuid4()),
                messages=[
                    AgentMessage(id=str(uuid.uuid4()), role="user", content=message),
                ],
            ),
        )
        raw = await self._stream_sse_authed(
            RUN_PATH,
            envelope.model_dump(by_alias=True),
            timeout=timeout,
            on_event=on_event,
        )
        return [AgentEvent.model_validate(e) for e in raw]
