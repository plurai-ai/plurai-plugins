# pyright: reportPrivateUsage=false
"""Tool-level tests against a mocked Pluto backend."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pluto_judge.config import AGENT_API, PLUTO_API
from pluto_judge.errors import safe_error_body
from pluto_judge.tools.classifiers import (
    GetResultsArgs,
    SearchEvaluatorsArgs,
    _get_results,
    _search_evaluators,
)
from pluto_judge.tools.judge import (
    AskUserArgs,
    AskUserOption,
    AskUserQuestion,
    SendMessageArgs,
    StartJudgeArgs,
    _ask_user,
    _needs_input_template,
    _normalize_name,
    _send_message,
    _start_judge,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _sse_body(events: list[dict[str, Any]]) -> bytes:
    """Encode events as a Server-Sent Events stream body."""
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


# ── Pure helpers (no network) ────────────────────────────────────────────


def test_normalize_name_truncates_word_count() -> None:
    assert _normalize_name("a b c d e f g") == "a b c d e"


def test_normalize_name_truncates_long_strings() -> None:
    name = "x" * 60
    assert len(_normalize_name(name)) <= 50


def test_needs_input_template_detects_grounding() -> None:
    assert _needs_input_template("Classify whether response is grounded in context")


def test_needs_input_template_skipped_when_template_present() -> None:
    assert not _needs_input_template(
        "Grounding. Input format: '## Context:\\n{c}\\n\\n## Response:\\n{r}'"
    )


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
    httpx_mock.add_response(url=f"{PLUTO_API}/classifiers", method="GET", json={"items": items})
    # Per-classifier "has_optimization" probes — return 404 so flag = False.
    for slug in (f"id-{i}" for i in range(2)):
        httpx_mock.add_response(
            url=f"{PLUTO_API}/classifiers/{slug}/versions/1.0.0/optimization",
            method="GET",
            status_code=404,
        )
    for slug in (f"slug-{i}" for i in range(2)):
        httpx_mock.add_response(
            url=f"{PLUTO_API}/classifiers/{slug}/versions/1.0.0/optimization",
            method="GET",
            status_code=404,
        )

    md = await _search_evaluators(
        SearchEvaluatorsArgs(limit=2, offset=0, response_format="markdown"), ctx
    )
    assert isinstance(md, str)
    assert "eval-0" in md and "eval-1" in md and "eval-2" not in md


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
    httpx_mock.add_response(url=f"{PLUTO_API}/classifiers", method="GET", json={"items": items})
    httpx_mock.add_response(
        url=f"{PLUTO_API}/classifiers/id-0/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
    )
    httpx_mock.add_response(
        url=f"{PLUTO_API}/classifiers/slug-0/versions/1.0.0/optimization",
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
        url=f"{PLUTO_API}/classifiers/c-1",
        method="GET",
        json={"slug": "s-1", "defaultVersion": {"number": "1.0.0"}},
    )
    httpx_mock.add_response(
        url=f"{PLUTO_API}/classifiers/c-1/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
    )
    httpx_mock.add_response(
        url=f"{PLUTO_API}/classifiers/s-1/versions/1.0.0/optimization",
        method="GET",
        status_code=404,
    )
    out = await _get_results(GetResultsArgs(classifier_id="c-1", response_format="json"), ctx)
    assert isinstance(out, dict)
    assert out["baseline"] == {"accuracy": None, "precision": None, "recall": None}
    assert out["optimized"] == {"accuracy": None, "precision": None, "recall": None}


# ── start_judge happy path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_judge_happy_path(httpx_mock: Any, ctx: Any) -> None:
    httpx_mock.add_response(
        url=f"{PLUTO_API}/threads",
        method="POST",
        json={"id": "thread-1", "exampleSetId": "es-1"},
    )
    httpx_mock.add_response(url=f"{PLUTO_API}/threads/thread-1", method="PATCH", json={})
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

    out = await _start_judge(
        StartJudgeArgs(
            name="abc def ghi jkl mno pqr stu",  # > 5 words; should truncate
            task_description="Classify outputs as safe or unsafe",
        ),
        ctx,
    )
    assert out["thread_id"] == "thread-1"
    assert out["example_set_id"] == "es-1"
    assert out["action_required"] == "PRESENT_QUESTIONS_TO_USER"
    assert out["agent_response"] == "What labels?"
    assert ctx.request_context.lifespan_context.has_questions is True


@pytest.mark.asyncio
async def test_start_judge_blocks_multifield_without_template(ctx: Any) -> None:
    out = await _start_judge(
        StartJudgeArgs(
            name="grounding eval",
            task_description="Detect when response is not grounded in context",
        ),
        ctx,
    )
    assert "error" in out
    assert "input template" in out["error"].lower()


# ── ask_user: gating + decline-fallback ──────────────────────────────────


@pytest.mark.asyncio
async def test_ask_user_requires_start_judge_first(ctx: Any) -> None:
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
async def test_ask_user_falls_back_when_elicit_declined(ctx: Any) -> None:
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
    assert out["action"] == "elicitation_unavailable"
    assert out["fallback"] == "AskUserQuestion"
    # has_questions should be reset whether we accepted or fell back.
    assert ctx.request_context.lifespan_context.has_questions is False
