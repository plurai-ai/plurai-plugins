# pyright: reportPrivateUsage=false
"""Tool-level tests against a mocked Plurai backend."""

from __future__ import annotations

import contextlib
import json
from typing import Any

import httpx
import pytest

from evals_mcp.clients.agent import _INTERMEDIATE_STATE_EVENT_NAME
from evals_mcp.config import get_settings
from evals_mcp.errors import format_tool_error, safe_error_body
from evals_mcp.tools.classifiers import (
    GetResultsArgs,
    SearchEvaluatorsArgs,
    _get_results,
    _search_evaluators,
)
from evals_mcp.tools.evaluator import (
    AskUserArgs,
    AskUserOption,
    AskUserQuestion,
    SendMessageArgs,
    StartEvaluatorArgs,
    _ask_user,
    _send_message,
    _start_evaluator,
)

from .conftest import FakeLangGraphClient, FakeStreamPart

_settings = get_settings()
PLATFORM_API = _settings.platform_api

# ── Helpers ──────────────────────────────────────────────────────────────


def _state_event(state: dict[str, Any]) -> FakeStreamPart:
    """Build an ``events`` stream envelope wrapping the agent's mid-run
    state-snapshot custom event.

    The agent emits these events to surface state mid-stream while a run
    is still going. Used by the optimize fast-path which reads
    ``classifier_id`` via the ``on_state`` callback before the SSE stream
    finishes; tests that instead need final messages/commit_id should
    configure the fake's ``threads.get_state`` via
    :meth:`FakeLangGraphClient.set_state`.
    """
    return FakeStreamPart(
        event="events",
        data={
            "event": "on_custom_event",
            "name": _INTERMEDIATE_STATE_EVENT_NAME,
            "data": state,
        },
    )


# ── Pure helpers (no network) ────────────────────────────────────────────


def test_safe_error_body_redacts_secrets() -> None:
    response = httpx.Response(
        500,
        content=json.dumps({"authorization": "Bearer abc", "msg": "boom"}).encode(),
    )
    request = httpx.Request("GET", "https://example.com")
    err = httpx.HTTPStatusError("e", request=request, response=response)
    body = safe_error_body(err)
    assert "abc" not in body
    assert "[redacted]" in body
    assert "boom" in body


def test_format_tool_error_handles_langgraph_sdk_connection_error() -> None:
    """``langgraph_sdk.errors.APIConnectionError`` extends
    ``httpx.HTTPStatusError`` but is constructed with ``response=None``.
    A naive ``exc.response.status_code`` access would raise AttributeError —
    the user would see ``'NoneType' object has no attribute 'status_code'``
    instead of a meaningful network error."""
    from langgraph_sdk.errors import APIConnectionError

    request = httpx.Request("POST", "https://run.plurai.ai/threads/x/runs/stream")
    err = APIConnectionError(request=request)
    out = format_tool_error(err)
    assert "Network error reaching Plurai" in out["error"]


def test_format_tool_error_returns_login_prompt_for_missing_api_key() -> None:
    """The whole inline-auth flow hinges on the wrapper's envelope carrying
    the ``auth login`` instruction. A future refactor that narrowed the tool
    wrapper's ``except`` clause to drop ``RuntimeError`` would silently break
    this UX — pin the envelope shape end-to-end."""
    from evals_mcp.errors import MissingApiKeyError

    out = format_tool_error(MissingApiKeyError())
    assert "auth login" in out["error"]
    assert "Plurai API key not set" in out["error"]


def test_format_tool_error_returns_login_prompt_for_corrupt_credentials() -> None:
    """Same UX contract as missing-key, but the message must name the broken
    file so the user can decide whether to delete or repair it."""
    from pathlib import Path

    from evals_mcp.errors import CorruptCredentialsError

    out = format_tool_error(CorruptCredentialsError(Path("/tmp/x"), "invalid JSON"))
    assert "auth login" in out["error"]
    assert "/tmp/x" in out["error"]
    assert "invalid JSON" in out["error"]


# ── Send-message guards ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_rejects_bare_optimize(ctx: Any) -> None:
    out = await _send_message(SendMessageArgs(thread_id="t1", message="Optimize"), ctx)
    assert "error" in out
    assert "Optimize [LLM]" in out["error"]


async def _drain_background(state: Any) -> None:
    """Drain any background optimize tasks the test spawned so they don't
    outlive the httpx_mock client."""
    for task in list(state.background_tasks):
        with contextlib.suppress(Exception):
            await task


@pytest.mark.asyncio
async def test_optimize_returns_classifier_id_for_round_trip(
    httpx_mock: Any, langgraph_client: FakeLangGraphClient, ctx: Any
) -> None:
    """The optimize response must echo classifier_id so the orchestrator can
    pass it back to evals_get_results on each wake-up — that round-trip is
    the only durable handoff (the MCP server is stateless across restarts)."""
    state = ctx.request_context.lifespan_context
    langgraph_client.set_frames([_state_event({"classifier_id": "cls-abc"})])
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/cls-abc",
        method="GET",
        json={"id": "cls-abc", "slug": "my-eval", "defaultVersion": {"number": "1.0.0"}},
    )

    out = await _send_message(
        SendMessageArgs(thread_id="thr-1", message="Optimize [SLM]"),
        ctx,
    )
    await _drain_background(state)

    assert out["status"] == "optimization_started"
    assert out["classifier_id"] == "cls-abc"
    assert out["slug"] == "my-eval"
    assert out["version"] == "1.0.0"
    assert "/ioa/v1/my-eval/1.0.0" in out["endpoint_url"]


@pytest.mark.asyncio
async def test_optimize_propagates_background_error_before_classifier(
    monkeypatch: Any, langgraph_client: FakeLangGraphClient, ctx: Any
) -> None:
    """A background run that errors before emitting classifier_id surfaces
    the underlying cause to the foreground, not the generic "no classifier_id
    emitted" timeout. The tool wrapper then maps it via ``format_tool_error``
    (e.g. 401 → inline auth prompt) — without propagation, the orchestrator
    sees a misleading timeout and the inline auth flow never fires.
    """
    state = ctx.request_context.lifespan_context

    langgraph_client.set_frames([FakeStreamPart(event="error", data={"message": "server boom"})])

    # Budget large enough that a regression to "wait the full timeout"
    # would block the suite, but small enough that a passing run is instant.
    monkeypatch.setattr(get_settings(), "classifier_wait_timeout_s", 30.0)

    with pytest.raises(RuntimeError, match="Agent stream error"):
        await _send_message(
            SendMessageArgs(thread_id="thr-1", message="Optimize [SLM]"),
            ctx,
        )
    await _drain_background(state)


@pytest.mark.asyncio
async def test_optimize_propagates_missing_api_key_error(
    monkeypatch: Any, langgraph_client: FakeLangGraphClient, ctx: Any
) -> None:
    """When the API key disappears mid-session (file deleted or expired),
    ``BearerCache.headers`` raises ``MissingApiKeyError`` on the next stream
    open. Optimize must surface this exception so ``format_tool_error`` can
    return the inline auth prompt — the previous behaviour swallowed it and
    the orchestrator timed out without ever asking the user for a key.
    """
    from evals_mcp.errors import MissingApiKeyError

    state = ctx.request_context.lifespan_context
    _ = langgraph_client  # fixture activates the fake SDK client

    def _raise_missing() -> dict[str, str]:
        raise MissingApiKeyError()

    # Swap the agent client's headers provider so headers() raises on the
    # next stream open — same shape as a credentials file deleted mid-session.
    monkeypatch.setattr(state.agent, "_headers_provider", _raise_missing)
    monkeypatch.setattr(get_settings(), "classifier_wait_timeout_s", 30.0)

    with pytest.raises(MissingApiKeyError):
        await _send_message(
            SendMessageArgs(thread_id="thr-1", message="Optimize [SLM]"),
            ctx,
        )
    await _drain_background(state)


@pytest.mark.asyncio
async def test_optimize_ignores_background_error_after_classifier_emits(
    httpx_mock: Any, monkeypatch: Any, langgraph_client: FakeLangGraphClient, ctx: Any
) -> None:
    """Once classifier_id has surfaced the foreground returns a useful
    payload; a later background failure (server-side optimization can run
    for ~20 min and may drop) must not retroactively raise — the user
    can poll via ``evals_get_results``.
    """
    state = ctx.request_context.lifespan_context
    langgraph_client.set_frames(
        [
            _state_event({"classifier_id": "cls-late"}),
            FakeStreamPart(event="error", data={"message": "late boom"}),
        ]
    )
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/cls-late",
        method="GET",
        json={"id": "cls-late", "slug": "my-eval", "defaultVersion": {"number": "1.0.0"}},
    )
    monkeypatch.setattr(get_settings(), "classifier_wait_timeout_s", 30.0)

    out = await _send_message(
        SendMessageArgs(thread_id="thr-1", message="Optimize [SLM]"),
        ctx,
    )
    await _drain_background(state)

    assert out["status"] == "optimization_started"
    assert out["classifier_id"] == "cls-late"


@pytest.mark.asyncio
async def test_optimize_raises_when_classifier_never_emitted(
    monkeypatch: Any, langgraph_client: FakeLangGraphClient, ctx: Any
) -> None:
    """If the agent never emits a snapshot with classifier_id, the wait times
    out. Without an ID there's no programmatic recovery (the orchestrator
    can't poll get_results), so we surface a clean error envelope rather
    than a fake pending status — the orchestrator retries Optimize from
    scratch or surfaces the URL to the user."""
    state = ctx.request_context.lifespan_context

    # Shrink the wait budget so the timeout path executes near-instantly.
    # Resolve via ``get_settings()`` rather than the module-level ``_settings``
    # because test_lifespan fixtures call ``get_settings.cache_clear()``,
    # which makes the module-level reference stale relative to what
    # ``_send_message`` actually reads at call time.
    monkeypatch.setattr(get_settings(), "classifier_wait_timeout_s", 0.05)

    langgraph_client.set_frames([_state_event({"messages": []})])

    # _send_message raises RuntimeError; the registered tool wrapper would
    # convert that to a {"error": ...} envelope via format_tool_error.
    with pytest.raises(RuntimeError, match="no classifier_id emitted"):
        await _send_message(
            SendMessageArgs(thread_id="thr-1", message="Optimize [LLM]"),
            ctx,
        )
    await _drain_background(state)


@pytest.mark.asyncio
async def test_get_results_arms_ask_user_when_optimization_complete(
    httpx_mock: Any, ctx: Any
) -> None:
    """Once optimized.accuracy is non-null, the next step is the language
    ask_user — get_results must arm the gate. While results are pending
    (null accuracy), it must NOT arm the gate so the orchestrator is
    forced to re-schedule a wake-up rather than ask premature questions."""
    state = ctx.request_context.lifespan_context

    # Pending case → gate stays closed.
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/c-1",
        method="GET",
        json={"slug": "s-1", "defaultVersion": {"number": "1.0.0"}},
    )
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/c-1/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
    )
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/s-1/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
    )
    state.has_questions = False
    await _get_results(GetResultsArgs(classifier_id="c-1", response_format="json"), ctx)
    assert state.has_questions is False

    # Completed case → gate armed.
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/c-2",
        method="GET",
        json={"slug": "s-2", "defaultVersion": {"number": "1.0.0"}},
    )
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/c-2/versions/1.0.0/optimization",
        method="GET",
        json={
            "baseline": {"accuracy": 0.6, "precision": 0.6, "recall": 0.6},
            "optimized": {"accuracy": 0.9, "precision": 0.9, "recall": 0.9},
        },
    )
    state.has_questions = False
    await _get_results(GetResultsArgs(classifier_id="c-2", response_format="json"), ctx)
    assert state.has_questions is True


# ── search_evaluators pagination + format ────────────────────────────────


@pytest.mark.asyncio
async def test_search_evaluators_paginates_and_renders_markdown(httpx_mock: Any, ctx: Any) -> None:
    items = [
        {
            "id": f"id-{i}",
            "name": f"eval-{i}",
            "description": "",
            "slug": f"slug-{i}",
            "defaultVersion": {"number": "1.0.0"},
            "outputSchema": {"properties": {"label": {"enum": ["a", "b"]}}},
            "createdAt": "2026-01-01",
        }
        for i in range(5)
    ]
    httpx_mock.add_response(url=f"{PLATFORM_API}/classifiers", method="GET", json={"items": items})
    # Per-classifier "has_optimization" probes — return 404 so flag = False.
    for slug in (f"id-{i}" for i in range(2)):
        httpx_mock.add_response(
            url=f"{PLATFORM_API}/classifiers/{slug}/versions/1.0.0/optimization",
            method="GET",
            status_code=404,
        )
    for slug in (f"slug-{i}" for i in range(2)):
        httpx_mock.add_response(
            url=f"{PLATFORM_API}/classifiers/{slug}/versions/1.0.0/optimization",
            method="GET",
            status_code=404,
        )

    md = await _search_evaluators(
        SearchEvaluatorsArgs(limit=2, offset=0, response_format="markdown"), ctx
    )
    assert isinstance(md, str)
    assert "eval-0" in md and "eval-1" in md and "eval-2" not in md
    # Header must scope the result to the user's own workspace, not a shared library.
    assert "in your Plurai workspace" in md


@pytest.mark.asyncio
async def test_search_evaluators_empty_state_frames_as_personal_collection(
    httpx_mock: Any, ctx: Any
) -> None:
    """An empty workspace must not read like a plugin failure — it should
    tell the model to proceed silently to creation."""
    httpx_mock.add_response(url=f"{PLATFORM_API}/classifiers", method="GET", json={"items": []})
    md = await _search_evaluators(
        SearchEvaluatorsArgs(limit=25, offset=0, response_format="markdown"), ctx
    )
    assert isinstance(md, str)
    assert "no existing evaluators" in md
    assert "normal for a new account" in md
    assert "proceed" in md.lower()
    # Empty result must NOT arm the ask_user gate — the model would otherwise
    # be free to invent its own pre-flow questions.
    assert ctx.request_context.lifespan_context.has_questions is False


@pytest.mark.asyncio
async def test_search_evaluators_arms_ask_user_gate_when_matches_exist(
    httpx_mock: Any, ctx: Any
) -> None:
    """When matching evaluators are returned, the model needs to call
    evals_ask_user to ask reuse-vs-create-new. Search must arm has_questions
    so that ask_user passes its gate."""
    items = [
        {
            "id": "id-0",
            "name": "eval-0",
            "description": "",
            "slug": "slug-0",
            "defaultVersion": {"number": "1.0.0"},
            "outputSchema": {"properties": {"label": {"enum": ["a"]}}},
            "createdAt": "2026-01-01",
        }
    ]
    httpx_mock.add_response(url=f"{PLATFORM_API}/classifiers", method="GET", json={"items": items})
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/id-0/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
    )
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/slug-0/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
    )

    state = ctx.request_context.lifespan_context
    state.has_questions = False
    await _search_evaluators(
        SearchEvaluatorsArgs(limit=25, offset=0, response_format="markdown"), ctx
    )
    assert state.has_questions is True


@pytest.mark.asyncio
async def test_search_evaluators_json_format(httpx_mock: Any, ctx: Any) -> None:
    items = [
        {
            "id": "id-0",
            "name": "eval-0",
            "description": "x",
            "slug": "slug-0",
            "defaultVersion": {"number": "1.0.0"},
            "outputSchema": {"properties": {"label": {"enum": ["a"]}}},
            "createdAt": "2026-01-01",
        }
    ]
    httpx_mock.add_response(url=f"{PLATFORM_API}/classifiers", method="GET", json={"items": items})
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/id-0/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
    )
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/slug-0/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
    )

    payload = await _search_evaluators(
        SearchEvaluatorsArgs(limit=10, offset=0, response_format="json"), ctx
    )
    assert isinstance(payload, dict)
    assert payload["count"] == 1
    assert payload["evaluators"][0]["has_optimization"] is False


# ── get_results: 404-on-both falls through to empty metrics ──────────────


@pytest.mark.asyncio
async def test_get_results_returns_empty_metrics_when_no_optimization(
    httpx_mock: Any, ctx: Any
) -> None:
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/c-1",
        method="GET",
        json={"slug": "s-1", "defaultVersion": {"number": "1.0.0"}},
    )
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/c-1/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
    )
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/s-1/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
    )
    out = await _get_results(GetResultsArgs(classifier_id="c-1", response_format="json"), ctx)
    assert isinstance(out, dict)
    assert out["baseline"] == {"accuracy": None, "precision": None, "recall": None}
    assert out["optimized"] == {"accuracy": None, "precision": None, "recall": None}


@pytest.mark.asyncio
async def test_get_results_requires_classifier_id() -> None:
    """The arg is required — pydantic must reject construction without it
    so the orchestrator is forced to round-trip the ID via conversation
    context rather than rely on per-process state."""
    with pytest.raises(ValueError):
        GetResultsArgs.model_validate({"response_format": "json"})
    with pytest.raises(ValueError):
        GetResultsArgs.model_validate({"classifier_id": "", "response_format": "json"})


# ── start_evaluator happy path ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_evaluator_happy_path(
    httpx_mock: Any, langgraph_client: FakeLangGraphClient, ctx: Any
) -> None:
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/threads",
        method="POST",
        json={"id": "thread-1", "exampleSetId": "es-1"},
    )
    langgraph_client.set_state(
        {
            "messages": [
                {"role": "user", "content": "task"},
                {"role": "assistant", "content": "What labels?"},
            ]
        }
    )

    # Seed leftover state from a prior evaluator on the same server — the
    # post-commit branch of _send_message keys off this flag, so a leaked
    # True would mis-route the first follow-up of the new evaluator.
    ctx.request_context.lifespan_context.committed = True

    out = await _start_evaluator(
        StartEvaluatorArgs(task_description="Classify outputs as safe or unsafe"),
        ctx,
    )
    assert out["thread_id"] == "thread-1"
    assert out["example_set_id"] == "es-1"
    assert "action_required" not in out
    assert out["agent_response"] == "What labels?"
    assert "platform_constraint" in out
    assert "FROZEN" in out["platform_constraint"]
    assert "evals_start_evaluator" in out["platform_constraint"]
    assert ctx.request_context.lifespan_context.has_questions is True
    assert ctx.request_context.lifespan_context.committed is False


# ── ask_user: gating + decline-fallback ──────────────────────────────────


@pytest.mark.asyncio
async def test_ask_user_requires_start_evaluator_first(ctx: Any) -> None:
    out = await _ask_user(
        AskUserArgs(
            questions=[
                AskUserQuestion(
                    question="Pick one",
                    options=[AskUserOption(label="A", value="a")],
                )
            ]
        ),
        ctx,
    )
    assert "error" in out


@pytest.mark.asyncio
async def test_ask_user_returns_ask_user_question_payload(ctx: Any) -> None:
    """ask_user always returns a payload that instructs the model to call
    the host's AskUserQuestion tool — we don't use MCP elicitation at all."""
    ctx.request_context.lifespan_context.has_questions = True
    out = await _ask_user(
        AskUserArgs(
            questions=[
                AskUserQuestion(
                    question="LLM or SLM?",
                    options=[
                        AskUserOption(label="LLM", value="LLM"),
                        AskUserOption(label="SLM", value="SLM"),
                    ],
                )
            ]
        ),
        ctx,
    )
    assert out["action"] == "ask_user_question"
    assert "AskUserQuestion" in out["instructions"]
    assert len(out["askUserQuestions"]) == 1
    assert {o["label"] for o in out["askUserQuestions"][0]["options"]} == {"LLM", "SLM"}
    assert ctx.request_context.lifespan_context.has_questions is False


# ── ask_user: SLM/LLM step ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_user_allowed_for_slm_llm_step(ctx: Any) -> None:
    """The SLM/LLM step reaches ask_user the same way refinement does:
    the prior send_message re-armed has_questions. ``committed`` adds the
    Optimize-[LLM]/Optimize-[SLM] follow-up hint to instructions."""
    state = ctx.request_context.lifespan_context
    state.has_questions = True
    state.committed = True

    out = await _ask_user(
        AskUserArgs(
            questions=[
                AskUserQuestion(
                    question="LLM or SLM?",
                    options=[
                        AskUserOption(label="LLM", value="LLM"),
                        AskUserOption(label="SLM", value="SLM"),
                    ],
                )
            ]
        ),
        ctx,
    )
    assert "error" not in out
    assert out["action"] == "ask_user_question"
    assert "Optimize [LLM]" in out["instructions"]
    assert "Optimize [SLM]" in out["instructions"]


# ── send_message: surfaces url + instruction when initial flow completes ─


@pytest.mark.asyncio
async def test_send_message_surfaces_url_when_commit_id_present(
    langgraph_client: FakeLangGraphClient, ctx: Any
) -> None:
    """Reproduces the post-data-generation step: the agent emits a
    ``commit_id`` in state to mark the synthetic example set as committed.
    The response must surface a thread URL and a follow-up instruction, and
    re-arm has_questions for the next ask_user call."""
    langgraph_client.set_state(
        {
            "messages": [
                {"role": "user", "content": "answers"},
                {
                    "role": "assistant",
                    "content": "I've generated 16 synthetic examples for testing.",
                },
            ],
            "commit_id": "commit-abc",
        }
    )

    state = ctx.request_context.lifespan_context
    state.has_questions = False
    state.committed = False

    out = await _send_message(
        SendMessageArgs(thread_id="thread-1", message="Yes, labels are fine."),
        ctx,
    )

    assert "url" in out
    assert out["url"].endswith("/thread/thread-1")
    assert "instructions" in out
    instructions = out["instructions"]
    # Must direct the orchestrator to surface the URL and ask SLM vs LLM in
    # the same turn — no separate review-confirmation gate.
    assert "review/edit" in instructions
    assert "evals_ask_user" in instructions
    assert "SLM" in instructions and "LLM" in instructions
    assert "Ready to optimize" not in instructions
    assert "review-confirm" not in instructions.lower()
    # Frozen-task constraint must be surfaced post-commit, so a user who asks
    # to "change the task" after seeing samples gets a restart, not a silent
    # sample-only edit.
    assert "platform_constraint" in out
    assert "FROZEN" in out["platform_constraint"]
    assert "evals_start_evaluator" in out["platform_constraint"]
    assert state.committed is True
    # Re-armed so the next ask_user (optimization choice) is allowed through.
    assert state.has_questions is True


@pytest.mark.asyncio
async def test_send_message_no_url_when_no_commit_id(
    langgraph_client: FakeLangGraphClient, ctx: Any
) -> None:
    """During refinement (no ``commit_id`` yet), no url is surfaced —
    that's reserved for the post-data-generation transition."""
    langgraph_client.set_state(
        {
            "messages": [
                {"role": "user", "content": "task"},
                {"role": "assistant", "content": "What labels do you want?"},
            ]
        }
    )

    state = ctx.request_context.lifespan_context
    state.has_questions = False
    state.committed = False

    out = await _send_message(SendMessageArgs(thread_id="thread-1", message="more context"), ctx)
    assert "url" not in out
    # Frozen-task constraint is only meaningful post-commit; the refinement
    # branch must not leak it.
    assert "platform_constraint" not in out
    # Pre-commit branch now carries WHO-answers guidance — pin the contract
    # by name (evals_ask_user) without freezing the wording.
    assert "instructions" in out
    assert "evals_ask_user" in out["instructions"]
    assert state.committed is False
    # Refinement question detected → has_questions re-armed via the '?' branch.
    assert state.has_questions is True
