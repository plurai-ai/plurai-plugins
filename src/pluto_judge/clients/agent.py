"""Typed CopilotKit agent client.

The Pluto agent endpoint at ``/api/agent/api/copilotkit`` accepts a
single envelope shape and replies with a Server-Sent Events stream of
``MESSAGES_SNAPSHOT`` / ``STATE_SNAPSHOT`` / etc. events. Tools call
:meth:`AgentClient.run_agent`, get back a list of typed events, and
walk them with helpers in ``tools/judge.py``.
"""

from __future__ import annotations

import uuid

from .base import BaseHttpClient
from .models import AgentEnvelope, AgentEvent, AgentMessage, AgentRunBody


class AgentClient(BaseHttpClient):
    """Async client for the Pluto agent (CopilotKit) endpoint."""

    _client_label = "Pluto Agent"

    async def run_agent(
        self,
        thread_id: str,
        message: str,
        *,
        run_id: str | None = None,
        timeout: float | None = None,
    ) -> list[AgentEvent]:
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
            "/copilotkit",
            envelope.model_dump(by_alias=True),
            timeout=timeout,
        )
        return [AgentEvent.model_validate(e) for e in raw]
