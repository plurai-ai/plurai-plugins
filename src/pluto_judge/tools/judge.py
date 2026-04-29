"""Judge-flow tools: start_judge, send_message, ask_user."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Annotated, Any, Literal, cast

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, create_model

from ..config import AGENT_API, DASHBOARD_BASE, PLUTO_API
from ..errors import safe_error_body
from ..state import ServerState

# ── Input models ──────────────────────────────────────────────────────────

_StrictModel = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StartJudgeArgs(BaseModel):
    model_config = _StrictModel
    name: Annotated[
        str, Field(description="Short name (2-5 words, e.g. 'health advice detection').")
    ]
    task_description: Annotated[
        str,
        Field(
            description=(
                "1-2 sentences, max 150 chars. Include task + desired label names. "
                "No examples or criteria. "
                "Example: 'Classify responses as health_advice or safe.'"
            ),
        ),
    ]


class SendMessageArgs(BaseModel):
    model_config = _StrictModel
    thread_id: Annotated[str, Field(description="Thread ID returned by pluto_start_judge.")]
    message: Annotated[str, Field(description="Message to send to the Pluto agent.")]


class AskUserOption(BaseModel):
    model_config = _StrictModel
    label: Annotated[str, Field(description="Display text for the option.")]
    value: Annotated[str, Field(description="Value returned when this option is selected.")]


class AskUserQuestion(BaseModel):
    model_config = _StrictModel
    question: Annotated[str, Field(description="The question text.")]
    options: Annotated[
        list[AskUserOption],
        Field(description="Selectable options for this question."),
    ]


class AskUserArgs(BaseModel):
    model_config = _StrictModel
    questions: Annotated[
        list[AskUserQuestion],
        Field(description="Array of questions to present to the user."),
    ]


# ── Helpers ──────────────────────────────────────────────────────────────


def _agent_payload(thread_id: str, message: str) -> dict[str, Any]:
    return {
        "method": "agent/run",
        "params": {"agentId": "agent"},
        "body": {
            "threadId": thread_id,
            "runId": str(uuid.uuid4()),
            "state": {},
            "messages": [
                {"id": str(uuid.uuid4()), "role": "user", "content": message},
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
    }


def _extract_conversation(events: list[dict[str, Any]]) -> tuple[list[dict[str, str]], str | None]:
    """Walk SSE events; return (conversation, classifier_id)."""
    conversation: list[dict[str, str]] = []
    classifier_id: str | None = None
    for event in events:
        etype = event.get("type", "")
        if etype == "MESSAGES_SNAPSHOT":
            conversation = [
                {"role": m["role"], "content": m["content"]}
                for m in event.get("messages", [])
                if m.get("content") and m["content"] != "..."
            ]
        elif etype == "STATE_SNAPSHOT":
            snapshot = event.get("snapshot", {})
            if isinstance(snapshot, dict):
                snap_dict = cast("dict[str, Any]", snapshot)
                cid = snap_dict.get("classifier_id")
                if isinstance(cid, str):
                    classifier_id = cid
    return conversation, classifier_id


def _last_assistant(conversation: list[dict[str, str]]) -> str:
    for msg in reversed(conversation):
        if msg["role"] == "assistant":
            return msg["content"]
    return ""


def _state_of(ctx: Context[Any, Any, Any]) -> ServerState:
    return cast(ServerState, ctx.request_context.lifespan_context)


# ── Optimization fast-path (pre-send check) ──────────────────────────────


async def _check_optimization_status(state: ServerState, thread_id: str) -> dict[str, Any] | None:
    """If a classifier already has results / is mid-optimization, return a
    short-circuit response. Returning None means the caller should send the
    message to the agent normally."""
    classifier_id = state.classifier_by_thread.get(thread_id)
    if not classifier_id:
        return None

    try:
        classifier = await state.pluto.request("GET", f"{PLUTO_API}/classifiers/{classifier_id}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    slug = classifier["slug"]
    version = classifier.get("defaultVersion", {}).get("number", "1.0.0")

    opt = await _fetch_optimization(state, classifier_id, slug, version)
    if not opt:
        return None

    baseline = opt.get("baseline", {})
    optimized = opt.get("optimized", {})

    from ..config import RUN_BASE  # local import to avoid circular at import-time

    if optimized and optimized.get("accuracy") is not None:
        return {
            "status": "already_optimized",
            "message": "Optimization was already completed. Here are the results.",
            "classifier_id": classifier_id,
            "slug": slug,
            "version": version,
            "endpoint_url": f"{RUN_BASE}/ioa/v1/{slug}/{version}",
            "baseline": _metrics(baseline),
            "optimized": _metrics(optimized),
        }
    if baseline and baseline.get("accuracy") is not None:
        return {
            "status": "optimization_in_progress",
            "message": (
                "Optimization is already running. Baseline results are available. "
                "Wait for optimization to complete, then call pluto_get_results."
            ),
            "classifier_id": classifier_id,
            "baseline": _metrics(baseline),
        }
    return None


async def _fetch_optimization(
    state: ServerState, classifier_id: str, slug: str, version: str
) -> dict[str, Any] | None:
    """Try classifier UUID first, then slug. 404 from both → no optimization run yet."""
    for identifier in (classifier_id, slug):
        try:
            return cast(
                dict[str, Any],
                await state.pluto.request(
                    "GET",
                    f"{PLUTO_API}/classifiers/{identifier}/versions/{version}/optimization",
                ),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                continue
            raise
    return None


def _metrics(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "accuracy": m.get("accuracy"),
        "precision": m.get("precision"),
        "recall": m.get("recall"),
    }


# ── Tool implementations ─────────────────────────────────────────────────


async def _start_judge(args: StartJudgeArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    task_description = args.task_description

    state = _state_of(ctx)

    thread = await state.pluto.request(
        "POST", f"{PLUTO_API}/threads", json_body={"workflow": "with-data"}
    )
    if "id" not in thread and "items" in thread:
        thread = thread["items"][0]
    thread_id: str = thread["id"]

    events = await state.agent.stream_sse(AGENT_API, _agent_payload(thread_id, task_description))
    conversation, _ = _extract_conversation(events)
    agent_response = _last_assistant(conversation)

    state.has_questions = True

    return {
        "thread_id": thread_id,
        "example_set_id": thread.get("exampleSetId", ""),
        "url": f"{DASHBOARD_BASE}/thread/{thread_id}",
        "agent_response": agent_response,
        "action_required": "PRESENT_QUESTIONS_TO_USER",
        "instructions": (
            "The agent returned refinement questions. "
            "First call ToolSearch with query 'pluto_ask_user' to load the tool, "
            "then call pluto_ask_user with the questions rephrased as options. "
            "Do NOT present the questions as text."
        ),
    }


async def _send_message(args: SendMessageArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    state = _state_of(ctx)
    thread_id = args.thread_id
    message = args.message

    if message.strip().lower() == "optimize":
        return {
            "error": (
                "Do not send 'Optimize' alone. You must send exactly "
                "'Optimize [LLM]' or 'Optimize [SLM]'."
            )
        }

    is_optimize = message.strip().lower().startswith("optimize")
    if is_optimize:
        status = await _check_optimization_status(state, thread_id)
        if status:
            return status

    payload = _agent_payload(thread_id, message)

    if is_optimize:
        # Fire-and-forget: optimisation can take ~2 min (LLM) or ~20 min (SLM);
        # don't block the tool call. Hold a reference so the task isn't GC'd.
        async def _run_optimize() -> None:
            # Fire-and-forget: failure surfaces when the user calls
            # pluto_get_results and sees no results.
            with contextlib.suppress(httpx.HTTPError, OSError):
                await state.agent.stream_sse(AGENT_API, payload, timeout=600.0)

        task = asyncio.create_task(_run_optimize())
        state.background_tasks.add(task)
        task.add_done_callback(state.background_tasks.discard)

        return {
            "status": "optimization_started",
            "message": (
                f"Optimization '{message}' triggered for thread {thread_id}. "
                "It runs in the background (~2 min for LLM, ~20 min for SLM). "
                "Use pluto_get_results later to check results."
            ),
            "thread_id": thread_id,
            "dashboard_url": f"{DASHBOARD_BASE}/thread/{thread_id}",
        }

    events = await state.agent.stream_sse(AGENT_API, payload)
    conversation, classifier_id = _extract_conversation(events)
    agent_response = _last_assistant(conversation)

    result: dict[str, Any] = {
        "agent_response": agent_response,
        "message_count": len(conversation),
    }
    if classifier_id:
        result["classifier_id"] = classifier_id
        state.classifier_by_thread[thread_id] = classifier_id

    if "?" in agent_response and not classifier_id:
        state.has_questions = True
        result["action_required"] = "PRESENT_QUESTIONS_TO_USER"
        result["instructions"] = (
            "The agent returned refinement questions. You MUST call pluto_ask_user to present "
            "them. Do NOT answer these questions yourself. Do NOT output any text before "
            "calling pluto_ask_user.\n\n"
            "FORMAT RULES:\n"
            "- Labels question: option 1 label = the EXACT label names from brackets joined "
            "with ' / ' plus '(Recommended)'. Option 2 = suggest SPECIFIC alternative label "
            "names relevant to the task (e.g. 'pass / fail', 'safe / unsafe', "
            "'grounded / hallucinated'). Do NOT just say 'Suggest different labels' — provide "
            "actual alternative names.\n"
            "- Other questions: 2-3 short options, labels under 8 words."
        )
    return result


# ── ask_user (elicitation) ───────────────────────────────────────────────


def _build_elicit_form(questions: list[AskUserQuestion]) -> type[BaseModel]:
    """Dynamically build a Pydantic form model from user-supplied questions.

    Each question becomes one `string` field; options are encoded as a JSON
    schema `enum` so the elicitation UI renders a picker.
    """
    fields: dict[str, Any] = {}
    for i, q in enumerate(questions):
        field_name = f"q{i + 1}"
        if q.options:
            allowed: list[Any] = [o.value for o in q.options]
            fields[field_name] = (
                str,
                Field(
                    ...,
                    title=q.question,
                    json_schema_extra={"enum": allowed},
                ),
            )
        else:
            fields[field_name] = (str, Field(..., title=q.question))
    return create_model(  # pyright: ignore[reportCallIssue, reportUnknownVariableType]
        "AskUserForm",
        __config__=_StrictModel,
        **fields,
    )


def _is_optimization_question(questions: list[AskUserQuestion]) -> bool:
    for q in questions:
        lowered = q.question.lower()
        if "optim" in lowered or "slm" in lowered or "llm" in lowered:
            return True
    return False


def _fallback_payload(questions: list[AskUserQuestion]) -> dict[str, Any]:
    """When elicitation is declined / unsupported, return a payload the model
    can forward to the host's AskUserQuestion tool."""
    ask_user_questions = [
        {
            "question": q.question,
            "header": q.question[:12],
            "options": [{"label": o.label, "description": o.value} for o in q.options],
            "multiSelect": False,
        }
        for q in questions
    ]
    extra = ""
    if _is_optimization_question(questions):
        extra = (
            " IMPORTANT: After the user chooses, call pluto_send_message with EXACTLY "
            "message='Optimize [LLM]' or message='Optimize [SLM]'. One call only. These are "
            "hardcoded strings — do not modify them."
        )
    return {
        "action": "elicitation_unavailable",
        "fallback": "AskUserQuestion",
        "instructions": (
            "Elicitation is not available in this environment. "
            "You MUST now call the AskUserQuestion tool with the questions below. "
            "Use ToolSearch to load it first if needed. Do NOT answer the questions yourself."
            + extra
        ),
        "askUserQuestions": ask_user_questions,
    }


async def _ask_user(args: AskUserArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    state = _state_of(ctx)
    if not state.has_questions:
        return {"error": ("You must call pluto_start_judge first. Do NOT ask your own questions.")}
    state.has_questions = False
    questions = args.questions

    form_model = _build_elicit_form(questions)
    try:
        result = await ctx.elicit(message="Please answer these questions:", schema=form_model)
    except Exception:
        # Host doesn't support elicitation (e.g. VS Code). Fall back.
        return _fallback_payload(questions)

    action: Any = getattr(result, "action", None)
    if action == "accept":
        data: Any = getattr(result, "data", None)
        data_dict: dict[str, Any] = {}
        dump = getattr(data, "model_dump", None)
        if callable(dump):
            data_dict = cast(dict[str, Any], dump())
        elif isinstance(data, dict):
            data_dict = cast(dict[str, Any], data)
        answers = {q.question: data_dict.get(f"q{i + 1}", "") for i, q in enumerate(questions)}
        return {"answers": answers, "action": "accepted"}

    return _fallback_payload(questions)


# ── Registration ─────────────────────────────────────────────────────────


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="pluto_start_judge",
        description=(
            "Start building an LLM-as-a-judge evaluator: creates a thread, sends the task "
            "to the Pluto agent, and returns refinement questions. This MUST be your first "
            "tool call."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def pluto_start_judge(
        args: StartJudgeArgs, ctx: Context[Any, Any, Any]
    ) -> dict[str, Any]:
        try:
            return await _start_judge(args, ctx)
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code}: {safe_error_body(e)}"}

    @mcp.tool(
        name="pluto_send_message",
        description=(
            "Send a follow-up message to the Pluto agent. Only use AFTER pluto_start_judge. "
            "For: sending user answers, 'Optimize [LLM]', 'Optimize [SLM]'."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def pluto_send_message(
        args: SendMessageArgs, ctx: Context[Any, Any, Any]
    ) -> dict[str, Any]:
        try:
            return await _send_message(args, ctx)
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code}: {safe_error_body(e)}"}

    @mcp.tool(
        name="pluto_ask_user",
        description=(
            "Present questions to the user via interactive form UI. Use this to ask refinement "
            "questions, optimization choices, or any decision that needs user input. Each "
            "question can have selectable options."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def pluto_ask_user(args: AskUserArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
        return await _ask_user(args, ctx)

    # Avoid unused-name warnings under strict linters.
    _ = (pluto_start_judge, pluto_send_message, pluto_ask_user, Literal)
