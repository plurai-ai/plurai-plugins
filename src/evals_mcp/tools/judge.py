"""Judge-flow tools: start_judge, send_message, ask_user.

`evals_send_message` includes a fast-path that short-circuits a duplicate
optimize when the classifier has already-completed or in-progress results.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal, TypedDict, cast

import httpx
import structlog
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, create_model

from ..clients import AgentEvent, GetClassifierResponse, OptimizationView
from ..config import get_settings
from ..errors import format_tool_error
from ..state import ServerState

logger: Any = structlog.get_logger(__name__)

# ── Input models ──────────────────────────────────────────────────────────

_StrictModel = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StartJudgeArgs(BaseModel):
    model_config = _StrictModel
    task_description: Annotated[
        str,
        Field(
            min_length=1,
            max_length=150,
            description=(
                "1-2 sentences, max 150 chars. Include task + desired label names. "
                "No examples or criteria. "
                "Example: 'Classify responses as health_advice or safe.'"
            ),
        ),
    ]


class SendMessageArgs(BaseModel):
    model_config = _StrictModel
    thread_id: Annotated[
        str, Field(min_length=1, description="Thread ID returned by evals_start_judge.")
    ]
    message: Annotated[str, Field(min_length=1, description="Message to send to the Plurai agent.")]


class AskUserOption(BaseModel):
    model_config = _StrictModel
    label: Annotated[str, Field(min_length=1, description="Display text for the option.")]
    value: Annotated[
        str, Field(min_length=1, description="Value returned when this option is selected.")
    ]


class AskUserQuestion(BaseModel):
    model_config = _StrictModel
    question: Annotated[str, Field(min_length=1, description="The question text.")]
    options: Annotated[
        list[AskUserOption],
        Field(description="Selectable options for this question."),
    ]


class AskUserArgs(BaseModel):
    model_config = _StrictModel
    questions: Annotated[
        list[AskUserQuestion],
        Field(min_length=1, description="Array of questions to present to the user."),
    ]


class ChatMessage(TypedDict):
    role: str
    content: str


# ── Helpers ──────────────────────────────────────────────────────────────


def _extract_conversation(events: list[AgentEvent]) -> list[ChatMessage]:
    """Pull the latest MESSAGES_SNAPSHOT from the event stream."""
    conversation: list[ChatMessage] = []
    for event in events:
        if event.type != "MESSAGES_SNAPSHOT":
            continue
        extra = event.model_dump(exclude={"type"})
        messages = cast(list[dict[str, Any]], extra.get("messages") or [])
        conversation = [
            ChatMessage(role=str(m.get("role", "")), content=str(m.get("content", "")))
            for m in messages
            if m.get("content") and m.get("content") != "..."
        ]
    return conversation


def _extract_classifier_id(events: list[AgentEvent]) -> str | None:
    """Pull the latest classifier_id from STATE_SNAPSHOT events, if any."""
    classifier_id: str | None = None
    for event in events:
        if event.type != "STATE_SNAPSHOT":
            continue
        extra = event.model_dump(exclude={"type"})
        snapshot = extra.get("snapshot")
        if not isinstance(snapshot, dict):
            continue
        cid = cast(dict[str, Any], snapshot).get("classifier_id")
        if isinstance(cid, str):
            classifier_id = cid
    return classifier_id


def _last_assistant(conversation: list[ChatMessage]) -> str:
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

    settings = get_settings()
    try:
        classifier: GetClassifierResponse = await state.platform.get_classifier(classifier_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    slug = classifier.slug
    version = classifier.default_version.number if classifier.default_version else "1.0.0"

    opt = await _fetch_optimization(state, classifier_id, slug, version)
    if opt is None:
        return None

    if opt.optimized.accuracy is not None:
        return {
            "status": "already_optimized",
            "message": "Optimization was already completed. Here are the results.",
            "classifier_id": classifier_id,
            "slug": slug,
            "version": version,
            "endpoint_url": f"{settings.run_url}/ioa/v1/{slug}/{version}",
            "baseline": opt.baseline.model_dump(),
            "optimized": opt.optimized.model_dump(),
        }
    if opt.baseline.accuracy is not None:
        return {
            "status": "optimization_in_progress",
            "message": (
                "Optimization is already running. Baseline results are available. "
                "Wait for optimization to complete, then call evals_get_results."
            ),
            "classifier_id": classifier_id,
            "slug": slug,
            "version": version,
            "endpoint_url": f"{settings.run_url}/ioa/v1/{slug}/{version}",
            "baseline": opt.baseline.model_dump(),
        }
    return None


async def _fetch_optimization(
    state: ServerState, classifier_id: str, slug: str, version: str
) -> OptimizationView | None:
    """Try classifier UUID first, then slug. None from both → no run yet."""
    for identifier in (classifier_id, slug):
        opt = await state.platform.get_optimization(identifier, version)
        if opt is not None:
            return opt
    return None


# ── Tool implementations ─────────────────────────────────────────────────


async def _start_judge(args: StartJudgeArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    state = _state_of(ctx)
    settings = get_settings()

    thread = await state.platform.create_thread()
    state.classifier_by_thread.pop(thread.id, None)

    events = await state.agent.run_agent(thread.id, args.task_description)
    conversation = _extract_conversation(events)
    agent_response = _last_assistant(conversation)

    state.has_questions = True

    return {
        "thread_id": thread.id,
        "example_set_id": thread.example_set_id,
        "url": f"{settings.api_base.rstrip('/')}/thread/{thread.id}",
        "agent_response": agent_response,
        "action_required": "PRESENT_QUESTIONS_TO_USER",
        "instructions": (
            "The agent returned refinement questions. "
            "Call evals_ask_user with the questions rephrased as options. "
            "Do NOT present the questions as text."
        ),
    }


async def _send_message(args: SendMessageArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    state = _state_of(ctx)
    settings = get_settings()
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

    if is_optimize:
        # Optimization runs ~2 min (LLM) / ~20 min (SLM). Run as fire-and-forget
        # so the tool call returns immediately; reference is held in
        # state.background_tasks so asyncio doesn't GC the task mid-flight.
        async def _run_optimize() -> None:
            try:
                await state.agent.run_agent(thread_id, message, timeout=600.0)
            except (httpx.HTTPError, OSError):
                logger.exception(
                    "Background optimize failed",
                    thread_id=thread_id,
                    message=message[:120],
                )

        task = asyncio.create_task(_run_optimize())
        state.background_tasks.add(task)
        task.add_done_callback(state.background_tasks.discard)

        return {
            "status": "optimization_started",
            "message": (
                f"Optimization '{message}' triggered for thread {thread_id}. "
                "It runs in the background (~2 min for LLM, ~20 min for SLM). "
                "Use evals_get_results later to check results."
            ),
            "thread_id": thread_id,
            "url": f"{settings.api_base.rstrip('/')}/thread/{thread_id}",
        }

    events = await state.agent.run_agent(thread_id, message)
    conversation = _extract_conversation(events)
    classifier_id = _extract_classifier_id(events)
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
            "The agent returned refinement questions. You MUST call evals_ask_user to present "
            "them. Do NOT answer these questions yourself. Do NOT output any text before "
            "calling evals_ask_user.\n\n"
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
            " IMPORTANT: After the user chooses, call evals_send_message with EXACTLY "
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
        return {"error": ("You must call evals_start_judge first. Do NOT ask your own questions.")}
    state.has_questions = False
    questions = args.questions

    form_model = _build_elicit_form(questions)
    try:
        result = await ctx.elicit(message="Please answer these questions:", schema=form_model)
    except McpError:
        # Host doesn't implement elicitation. Fall back.
        return _fallback_payload(questions)
    except Exception:
        # Unknown failure (transport, schema bug, internal). Log so we can
        # debug in environments that should support elicitation, and still
        # fall back so the user isn't stuck.
        logger.exception(
            "Elicitation failed unexpectedly; falling back",
            question_count=len(questions),
        )
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
        name="evals_start_judge",
        description=(
            "Start building an LLM-as-a-judge evaluator: creates a thread, sends the task "
            "to the Plurai agent, and returns refinement questions. This MUST be your first "
            "tool call."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def evals_start_judge(
        args: StartJudgeArgs, ctx: Context[Any, Any, Any]
    ) -> dict[str, Any]:
        try:
            return await _start_judge(args, ctx)
        except (httpx.HTTPStatusError, httpx.TransportError, RuntimeError) as e:
            return format_tool_error(e)

    @mcp.tool(
        name="evals_send_message",
        description=(
            "Send a follow-up message to the Plurai agent. Only use AFTER evals_start_judge. "
            "Used for sending user answers and for triggering optimization. "
            "To trigger optimization, send EXACTLY 'Optimize [LLM]' or 'Optimize [SLM]' "
            "(square brackets are literal). Optimization runs in the background — "
            "call evals_get_results to retrieve results."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def evals_send_message(
        args: SendMessageArgs, ctx: Context[Any, Any, Any]
    ) -> dict[str, Any]:
        try:
            return await _send_message(args, ctx)
        except (httpx.HTTPStatusError, httpx.TransportError, RuntimeError) as e:
            return format_tool_error(e)

    @mcp.tool(
        name="evals_ask_user",
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
    async def evals_ask_user(args: AskUserArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
        return await _ask_user(args, ctx)

    # Avoid unused-name warnings under strict linters.
    _ = (evals_start_judge, evals_send_message, evals_ask_user, Literal)
