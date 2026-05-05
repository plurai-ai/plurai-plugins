"""Tests for ``AgentClient``: SDK client lifecycle, header rotation,
stream/state parsing, and constructor input validation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from evals_mcp.clients import AgentClient
from evals_mcp.clients.agent import AgentMessage, ThreadStateView

from .conftest import FakeLangGraphClient, FakeStreamPart


@pytest.mark.asyncio
async def test_agent_client_caches_sdk_across_calls(
    langgraph_client: FakeLangGraphClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two consecutive operations with stable headers must share a single
    SDK client construction."""

    def _fake_get_client(*, url: str, headers: Any = None, timeout: Any = None) -> Any:
        del timeout
        langgraph_client.construction_calls.append({"url": url, "headers": dict(headers or {})})
        return langgraph_client

    monkeypatch.setattr("evals_mcp.clients.agent.get_client", _fake_get_client)

    def headers_provider() -> dict[str, str]:
        return {"X-User-Token": "ak_test"}

    async with AgentClient(
        base_url="http://langgraph.test",
        assistant_id="asst",
        timeout=10.0,
        headers_provider=headers_provider,
    ) as agent:
        await agent.get_state("t1")
        await agent.get_state("t2")

    assert len(langgraph_client.construction_calls) == 1


@pytest.mark.asyncio
async def test_agent_client_rebuilds_when_headers_change(
    langgraph_client: FakeLangGraphClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``headers_provider`` starts returning new values (e.g. after a
    mid-session `auth login` bumps the credentials file mtime), the SDK
    client must be rebuilt so subsequent requests use the fresh headers."""

    def _fake_get_client(*, url: str, headers: Any = None, timeout: Any = None) -> Any:
        del timeout
        langgraph_client.construction_calls.append({"url": url, "headers": dict(headers or {})})
        return langgraph_client

    monkeypatch.setattr("evals_mcp.clients.agent.get_client", _fake_get_client)

    current_headers = {"X-User-Token": "ak_old"}

    def headers_provider() -> dict[str, str]:
        return dict(current_headers)

    async with AgentClient(
        base_url="http://langgraph.test",
        assistant_id="asst",
        timeout=10.0,
        headers_provider=headers_provider,
    ) as agent:
        await agent.get_state("t1")
        current_headers["X-User-Token"] = "ak_new"
        await agent.get_state("t2")

    assert len(langgraph_client.construction_calls) == 2
    assert langgraph_client.construction_calls[0]["headers"] == {"X-User-Token": "ak_old"}
    assert langgraph_client.construction_calls[1]["headers"] == {"X-User-Token": "ak_new"}


def _patch_get_client(
    langgraph_client: FakeLangGraphClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_get_client(*, url: str, headers: Any = None, timeout: Any = None) -> Any:
        del timeout
        langgraph_client.construction_calls.append({"url": url, "headers": dict(headers or {})})
        return langgraph_client

    monkeypatch.setattr("evals_mcp.clients.agent.get_client", _fake_get_client)


@pytest.mark.asyncio
async def test_aexit_closes_cached_sdk_client(
    langgraph_client: FakeLangGraphClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each SDK client owns an httpx pool + transport with retries —
    leaking it on lifespan shutdown drains FDs in long-lived MCP servers."""
    _patch_get_client(langgraph_client, monkeypatch)

    async with AgentClient(
        base_url="http://langgraph.test",
        assistant_id="asst",
        timeout=10.0,
        headers_provider=lambda: {"Authorization": "Bearer x"},
    ) as agent:
        await agent.get_state("t1")
    assert langgraph_client.aclose_calls == 1


@pytest.mark.asyncio
async def test_header_rebuild_closes_previous_client(
    langgraph_client: FakeLangGraphClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A header rotation must close the prior SDK client; otherwise every
    mid-session `auth login` leaks a connection pool."""
    _patch_get_client(langgraph_client, monkeypatch)

    headers = {"Authorization": "Bearer old"}

    async with AgentClient(
        base_url="http://langgraph.test",
        assistant_id="asst",
        timeout=10.0,
        headers_provider=lambda: dict(headers),
    ) as agent:
        await agent.get_state("t1")
        assert langgraph_client.aclose_calls == 0
        headers["Authorization"] = "Bearer new"
        await agent.get_state("t2")
        # First rebuild closes the previous client.
        assert langgraph_client.aclose_calls == 1
    # __aexit__ closes the latest cached client.
    assert langgraph_client.aclose_calls == 2


@pytest.mark.asyncio
async def test_get_client_serialized_under_concurrent_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent ``_get_client`` callers across a header rotation must not
    leak SDK clients. Without serialization, two callers that race into the
    rebuild branch each construct a fresh client; one is overwritten in
    ``self._client`` and never ``aclose``-d.

    The fake's ``aclose`` yields via ``await asyncio.sleep(0)`` so the
    test would actually fail without the lock — every rebuild suspends,
    exposing the race window.
    """
    constructed: list[FakeLangGraphClient] = []

    class _SuspendingClient(FakeLangGraphClient):
        async def aclose(self) -> None:
            self.aclose_calls += 1
            await asyncio.sleep(0)

    def _fake_get_client(*, url: str, headers: Any = None, timeout: Any = None) -> Any:
        del url, headers, timeout
        client = _SuspendingClient()
        constructed.append(client)
        return client

    monkeypatch.setattr("evals_mcp.clients.agent.get_client", _fake_get_client)

    rotated = False

    def headers_provider() -> dict[str, str]:
        return {"Authorization": "Bearer new" if rotated else "Bearer old"}

    async with AgentClient(
        base_url="http://langgraph.test",
        assistant_id="asst",
        timeout=10.0,
        headers_provider=headers_provider,
    ) as agent:
        # Warm the cache pre-rotation via the public API.
        await agent.get_state("t-pre")
        assert len(constructed) == 1
        pre = constructed[0]
        assert pre.aclose_calls == 0

        # Rotate, then fire many concurrent callers straddling the rebuild.
        rotated = True
        await asyncio.gather(*(agent.get_state(f"t-{i}") for i in range(20)))

        # Exactly one rebuild — the lock collapsed the race.
        assert len(constructed) == 2, (
            f"expected one post-rotation rebuild, got {len(constructed) - 1}"
        )
        # The pre-rotation client was closed exactly once.
        assert pre.aclose_calls == 1
        post = constructed[1]
        assert post.aclose_calls == 0

    # __aexit__ closes the latest cached client.
    assert constructed[1].aclose_calls == 1


@pytest.mark.asyncio
async def test_stream_error_envelope_raises(
    langgraph_client: FakeLangGraphClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server-side ``error`` envelope mid-stream must raise — otherwise a
    failed run masquerades as silent completion."""
    _patch_get_client(langgraph_client, monkeypatch)
    langgraph_client.set_frames([FakeStreamPart(event="error", data={"message": "boom"})])

    async with AgentClient(
        base_url="http://langgraph.test",
        assistant_id="asst",
        timeout=10.0,
        headers_provider=lambda: {"Authorization": "Bearer x"},
    ) as agent:
        with pytest.raises(RuntimeError, match="Agent stream error"):
            await agent.run_agent("thr", "hi")


@pytest.mark.asyncio
async def test_stream_ignores_unrelated_events(
    langgraph_client: FakeLangGraphClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``on_state`` must fire only for the intermediate-state custom event;
    other event frames (metadata, values, mismatched custom-event names)
    must be ignored."""
    _patch_get_client(langgraph_client, monkeypatch)
    captured: list[ThreadStateView] = []

    langgraph_client.set_frames(
        [
            FakeStreamPart(event="metadata", data={"run_id": "r-1"}),
            FakeStreamPart(event="values", data={"messages": []}),
            FakeStreamPart(event="events", data={"event": "on_chain_start", "data": {}}),
            FakeStreamPart(
                event="events",
                data={"event": "on_custom_event", "name": "heartbeat", "data": None},
            ),
            FakeStreamPart(
                event="events",
                data={
                    "event": "on_custom_event",
                    "name": "copilotkit_manually_emit_intermediate_state",
                    "data": {"classifier_id": "cls-x"},
                },
            ),
        ]
    )

    async with AgentClient(
        base_url="http://langgraph.test",
        assistant_id="asst",
        timeout=10.0,
        headers_provider=lambda: {"Authorization": "Bearer x"},
    ) as agent:
        await agent.run_agent("thr", "hi", on_state=captured.append)

    assert len(captured) == 1
    assert captured[0].classifier_id == "cls-x"


def test_agent_message_normalizes_lc_type_to_role() -> None:
    """The agent SDK serializes messages with ``type``; our consumers
    only check ``role``. The normalization is the only reason
    ``last_assistant_message`` works on real-world payloads."""
    assert AgentMessage.model_validate({"type": "ai", "content": "hi"}).role == "assistant"
    assert AgentMessage.model_validate({"type": "human", "content": "hi"}).role == "user"
    assert AgentMessage.model_validate({"type": "tool", "content": "x"}).role == "tool"
    # Explicit ``role`` wins over ``type``.
    msg = AgentMessage.model_validate({"role": "assistant", "type": "human", "content": "x"})
    assert msg.role == "assistant"
    # Multimodal content gets coerced to a string so callers don't crash.
    multi = AgentMessage.model_validate(
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    )
    assert isinstance(multi.content, str)


def test_last_assistant_message_skips_placeholder_and_empties() -> None:
    """The agent emits ``"..."`` before tool calls and may emit empty
    content; both must be skipped so the user never sees a placeholder."""
    view = ThreadStateView.model_validate(
        {
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "real answer"},
                {"role": "assistant", "content": "..."},
                {"role": "assistant", "content": ""},
            ]
        }
    )
    assert view.last_assistant_message() == "real answer"

    only_placeholder = ThreadStateView.model_validate(
        {"messages": [{"role": "assistant", "content": "..."}]}
    )
    assert only_placeholder.last_assistant_message() == ""

    no_assistant = ThreadStateView.model_validate({"messages": [{"role": "user", "content": "hi"}]})
    assert no_assistant.last_assistant_message() == ""


@pytest.mark.asyncio
async def test_fetch_state_no_checkpoint_yet_returns_empty_view(
    langgraph_client: FakeLangGraphClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread with no checkpoint yet (first call before any run) returns
    ``values=None`` — that's a legitimate empty view, not a shape regression."""
    _patch_get_client(langgraph_client, monkeypatch)

    async def _no_values_get_state(thread_id: str, **_: Any) -> Any:
        del thread_id
        return {"values": None, "next": []}

    monkeypatch.setattr(langgraph_client.threads, "get_state", _no_values_get_state)
    async with AgentClient(
        base_url="http://langgraph.test",
        assistant_id="asst",
        timeout=10.0,
        headers_provider=lambda: {"Authorization": "Bearer x"},
    ) as agent:
        view = await agent.get_state("t1")
        assert view.messages == []
        assert view.classifier_id is None


@pytest.mark.asyncio
async def test_fetch_state_raises_on_shape_regression(
    langgraph_client: FakeLangGraphClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the SDK ever changes shape (snapshot is not a dict, or ``values``
    is a non-dict non-None), surface the regression as a RuntimeError so a
    real upstream change is visible rather than silently empty assistant
    messages downstream."""
    _patch_get_client(langgraph_client, monkeypatch)

    async def _list_get_state(thread_id: str, **_: Any) -> Any:
        del thread_id
        return [{"unexpected": "list"}]

    monkeypatch.setattr(langgraph_client.threads, "get_state", _list_get_state)
    async with AgentClient(
        base_url="http://langgraph.test",
        assistant_id="asst",
        timeout=10.0,
        headers_provider=lambda: {"Authorization": "Bearer x"},
    ) as agent:
        with pytest.raises(RuntimeError, match="get_state response shape"):
            await agent.get_state("t1")

    async def _bad_values_get_state(thread_id: str, **_: Any) -> Any:
        del thread_id
        return {"values": "not a dict", "next": []}

    monkeypatch.setattr(langgraph_client.threads, "get_state", _bad_values_get_state)
    async with AgentClient(
        base_url="http://langgraph.test",
        assistant_id="asst",
        timeout=10.0,
        headers_provider=lambda: {"Authorization": "Bearer x"},
    ) as agent:
        with pytest.raises(RuntimeError, match="get_state values shape"):
            await agent.get_state("t1")


def test_agent_client_rejects_invalid_constructor_inputs() -> None:
    """Loss of the ``BaseHttpClientConfig`` Pydantic guard would silently
    accept ``base_url=""`` (which then fails inside ``get_client``) — keep
    the contract checked at construction so misuse fails fast."""
    with pytest.raises(ValueError):
        AgentClient(
            base_url="",
            assistant_id="asst",
            timeout=10.0,
            headers_provider=lambda: {},
        )
    with pytest.raises(ValueError):
        AgentClient(
            base_url="http://x",
            assistant_id="",
            timeout=10.0,
            headers_provider=lambda: {},
        )
    with pytest.raises(ValueError):
        AgentClient(
            base_url="http://x",
            assistant_id="asst",
            timeout=0,
            headers_provider=lambda: {},
        )
