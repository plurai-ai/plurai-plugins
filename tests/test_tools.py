# pyright: reportPrivateUsage=false
"""Tool-level tests against a mocked Plurai backend."""

from __future__ import annotations

import contextlib
import json
from typing import Any

import httpx
import pytest

from evals_mcp.clients.agent import RUN_PATH
from evals_mcp.config import get_settings
from evals_mcp.errors import safe_error_body
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

_settings = get_settings()
PLATFORM_API = _settings.platform_api
AGENT_API = f"{_settings.agent_api_base}{RUN_PATH}"

# ── Helpers ──────────────────────────────────────────────────────────────


def _sse_body(events: list[dict[str, Any]]) -> bytes:
    """Encode events as a Server-Sent Events stream body."""
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


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
async def test_optimize_returns_classifier_id_for_round_trip(httpx_mock: Any, ctx: Any) -> None:
    """The optimize response must echo classifier_id so the orchestrator can
    pass it back to evals_get_results on each wake-up — that round-trip is
    the only durable handoff (the MCP server is stateless across restarts)."""
    state = ctx.request_context.lifespan_context
    httpx_mock.add_response(
        url=AGENT_API,
        method="POST",
        content=_sse_body(
            [
                {
                    "type": "STATE_SNAPSHOT",
                    "snapshot": {"classifier_id": "cls-abc"},
                }
            ]
        ),
        headers={"content-type": "text/event-stream"},
    )
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/cls-abc",
        method="GET",
        json={"id": "cls-abc", "slug": "my-eval", "defaultVersion": {"number": "1.0.0"}},
    )
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/cls-abc/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
    )
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/classifiers/my-eval/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
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
async def test_optimize_raises_when_classifier_never_emitted(
    monkeypatch: Any, httpx_mock: Any, ctx: Any
) -> None:
    """If the agent never emits a STATE_SNAPSHOT with classifier_id, the
    wait times out. Without an ID there's no programmatic recovery (the
    orchestrator can't poll get_results), so we surface a clean error
    envelope rather than a fake pending status — the orchestrator retries
    Optimize from scratch or surfaces the URL to the user."""
    state = ctx.request_context.lifespan_context

    # Shrink the wait budget so the timeout path executes near-instantly.
    import evals_mcp.tools.evaluator as evaluator_module

    monkeypatch.setattr(evaluator_module, "_CLASSIFIER_WAIT_TIMEOUT_S", 0.05)

    httpx_mock.add_response(
        url=AGENT_API,
        method="POST",
        content=_sse_body([{"type": "MESSAGES_SNAPSHOT", "messages": []}]),
        headers={"content-type": "text/event-stream"},
    )

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
async def test_start_evaluator_happy_path(httpx_mock: Any, ctx: Any) -> None:
    httpx_mock.add_response(
        url=f"{PLATFORM_API}/threads",
        method="POST",
        json={"id": "thread-1", "exampleSetId": "es-1"},
    )
    httpx_mock.add_response(
        url=AGENT_API,
        method="POST",
        content=_sse_body(
            [
                {
                    "type": "MESSAGES_SNAPSHOT",
                    "messages": [
                        {"role": "user", "content": "task"},
                        {"role": "assistant", "content": "What labels?"},
                    ],
                }
            ]
        ),
        headers={"content-type": "text/event-stream"},
    )

    out = await _start_evaluator(
        StartEvaluatorArgs(task_description="Classify outputs as safe or unsafe"),
        ctx,
    )
    assert out["thread_id"] == "thread-1"
    assert out["example_set_id"] == "es-1"
    assert out["action_required"] == "PRESENT_QUESTIONS_TO_USER"
    assert out["agent_response"] == "What labels?"
    assert ctx.request_context.lifespan_context.has_questions is True


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
    the prior send_message re-armed has_questions. ``commit_id`` adds the
    Optimize-[LLM]/Optimize-[SLM] follow-up hint to instructions."""
    state = ctx.request_context.lifespan_context
    state.has_questions = True
    state.commit_id = "commit-xyz"

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
async def test_send_message_surfaces_url_when_commit_id_present(httpx_mock: Any, ctx: Any) -> None:
    """Reproduces the post-data-generation step: the agent emits a
    ``commit_id`` in STATE_SNAPSHOT to mark the synthetic example set as
    committed. The response must surface a thread URL and a follow-up
    instruction, and re-arm has_questions for the next ask_user call."""
    httpx_mock.add_response(
        url=AGENT_API,
        method="POST",
        content=_sse_body(
            [
                {
                    "type": "MESSAGES_SNAPSHOT",
                    "messages": [
                        {"role": "user", "content": "answers"},
                        {
                            "role": "assistant",
                            "content": "I've generated 16 synthetic examples for testing.",
                        },
                    ],
                },
                {
                    "type": "STATE_SNAPSHOT",
                    "snapshot": {"commit_id": "commit-abc"},
                },
            ]
        ),
        headers={"content-type": "text/event-stream"},
    )

    state = ctx.request_context.lifespan_context
    state.has_questions = False
    state.commit_id = None

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
    assert state.commit_id == "commit-abc"
    # Re-armed so the next ask_user (optimization choice) is allowed through.
    assert state.has_questions is True


@pytest.mark.asyncio
async def test_send_message_no_url_when_no_commit_id(httpx_mock: Any, ctx: Any) -> None:
    """During refinement (no ``commit_id`` yet), no url is surfaced —
    that's reserved for the post-data-generation transition."""
    httpx_mock.add_response(
        url=AGENT_API,
        method="POST",
        content=_sse_body(
            [
                {
                    "type": "MESSAGES_SNAPSHOT",
                    "messages": [
                        {"role": "user", "content": "task"},
                        {"role": "assistant", "content": "What labels do you want?"},
                    ],
                },
            ]
        ),
        headers={"content-type": "text/event-stream"},
    )

    state = ctx.request_context.lifespan_context
    state.has_questions = False
    state.commit_id = None

    out = await _send_message(SendMessageArgs(thread_id="thread-1", message="more context"), ctx)
    assert "url" not in out
    assert state.commit_id is None
    # Refinement question detected → has_questions re-armed via the '?' branch.
    assert state.has_questions is True
