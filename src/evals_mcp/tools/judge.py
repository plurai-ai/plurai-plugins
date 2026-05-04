"""Judge-flow tools: start_judge, send_message, ask_user.

`evals_send_message` includes a fast-path that short-circuits a duplicate
optimize when the classifier has already-completed or in-progress results.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict, cast

import httpx
import structlog
from mcp.server.elicitation import AcceptedElicitation
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, create_model

from ..clients import AgentEvent, GetClassifierResponse, OptimizationView
from ..config import Settings, get_settings
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


@dataclass(frozen=True)
class AgentStateSnapshot:
    """Latest values pulled from the agent's STATE_SNAPSHOT events.

    A non-null ``commit_id`` is the agent's signal that the initial flow
    (synthetic data generation) is complete — it's the ID of the committed
    example set the user can now review.
    """

    classifier_id: str | None = None
    commit_id: str | None = None


@dataclass(frozen=True)
class RunResult:
    conversation: list[ChatMessage]
    snapshot: AgentStateSnapshot


def _parse_run(events: list[AgentEvent]) -> RunResult:
    """Single pass over the event stream: latest MESSAGES_SNAPSHOT wins for
    conversation; latest STATE_SNAPSHOT values win for snapshot."""
    conversation: list[ChatMessage] = []
    classifier_id: str | None = None
    commit_id: str | None = None
    event_types: dict[str, int] = {}
    last_snapshot_keys: list[str] = []
    for event in events:
        event_types[event.type] = event_types.get(event.type, 0) + 1
        if event.type == "MESSAGES_SNAPSHOT":
            extra = event.model_dump(exclude={"type"})
            messages = cast(list[dict[str, Any]], extra.get("messages") or [])
            conversation = [
                ChatMessage(role=str(m.get("role", "")), content=str(m.get("content", "")))
                for m in messages
                if m.get("content") and m.get("content") != "..."
            ]
        elif event.type == "STATE_SNAPSHOT":
            extra = event.model_dump(exclude={"type"})
            snapshot = extra.get("snapshot")
            if not isinstance(snapshot, dict):
                continue
            snap = cast(dict[str, Any], snapshot)
            last_snapshot_keys = sorted(snap.keys())
            cid = snap.get("classifier_id")
            if isinstance(cid, str):
                classifier_id = cid
            commit = snap.get("commit_id")
            if isinstance(commit, str) and commit:
                commit_id = commit
    logger.info(
        "agent_run_parsed",
        event_types=event_types,
        last_snapshot_keys=last_snapshot_keys,
        classifier_id=classifier_id,
        commit_id=commit_id,
    )
    return RunResult(
        conversation=conversation,
        snapshot=AgentStateSnapshot(classifier_id=classifier_id, commit_id=commit_id),
    )


def _last_assistant(conversation: list[ChatMessage]) -> str:
    for msg in reversed(conversation):
        if msg["role"] == "assistant":
            return msg["content"]
    return ""


def _state_of(ctx: Context[Any, Any, Any]) -> ServerState:
    return cast(ServerState, ctx.request_context.lifespan_context)


# ── Optimization fast-path (pre-send check) ──────────────────────────────


async def _check_optimization_status(state: ServerState) -> dict[str, Any] | None:
    """If a classifier already has results / is mid-optimization, return a
    short-circuit response. Returning None means the caller should send the
    message to the agent normally."""
    classifier_id = state.classifier_id
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


async def _handle_optimize(
    state: ServerState, settings: Settings, thread_id: str, message: str
) -> dict[str, Any]:
    """Optimize fast-path: short-circuit if a classifier already has results
    or is mid-optimization; otherwise fire-and-forget the agent run.

    Optimization takes ~2 min (LLM) / ~20 min (SLM), so we don't await it —
    the task ref is held in state.background_tasks so asyncio doesn't GC it.
    """
    status = await _check_optimization_status(state)
    if status:
        return status

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


def _fallback_payload(questions: list[AskUserQuestion], state: ServerState) -> dict[str, Any]:
    """When elicitation is declined / unsupported, return a payload the model
    can forward to the host's AskUserQuestion tool.

    The optimization-step reminder is attached only after the initial flow
    is done (signalled by ``state.commit_id`` being set) — pre-commit the
    model is forwarding agent refinement questions and the reminder doesn't
    apply.
    """
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
    if state.commit_id is not None:
        # In the post-flow-done phase the model drives questions; the
        # most consequential one is SLM-vs-LLM where the follow-up
        # send_message must use EXACT formatting. Attach with conditional
        # framing so it's a no-op for the integration-code question.
        extra = (
            " IF this is the optimization-type question (SLM vs LLM), then after the user "
            "chooses, call evals_send_message with EXACTLY message='Optimize [LLM]' or "
            "message='Optimize [SLM]'. One call only. These are hardcoded strings — do not "
            "modify them."
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


# ── Tool implementations ─────────────────────────────────────────────────


async def _start_judge(args: StartJudgeArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    state = _state_of(ctx)
    settings = get_settings()

    thread = await state.platform.create_thread()
    state.classifier_id = None

    events = await state.agent.run_agent(thread.id, args.task_description)
    run = _parse_run(events)
    agent_response = _last_assistant(run.conversation)
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
    normalized = message.strip().lower()

    if normalized == "optimize":
        return {
            "error": (
                "Do not send 'Optimize' alone. You must send exactly "
                "'Optimize [LLM]' or 'Optimize [SLM]'."
            )
        }

    if normalized.startswith("optimize"):
        return await _handle_optimize(state, settings, thread_id, message)

    events = await state.agent.run_agent(thread_id, message)
    run = _parse_run(events)
    classifier_id = run.snapshot.classifier_id
    agent_response = _last_assistant(run.conversation)
    state.commit_id = run.snapshot.commit_id

    result: dict[str, Any] = {
        "agent_response": agent_response,
        "message_count": len(run.conversation),
    }
    if classifier_id:
        result["classifier_id"] = classifier_id
        state.classifier_id = classifier_id

    # Re-arm so the model can call evals_ask_user next, whether the agent
    # came back with another refinement question or just finished the
    # initial flow and the next step is the SLM/LLM choice.
    state.has_questions = True

    if state.commit_id is not None:
        result["url"] = f"{settings.api_base.rstrip('/')}/thread/{thread_id}"
        result["instructions"] = (
            "The synthetic examples are ready. You MUST do all three in this turn: "
            "(1) show the user the agent_response text verbatim; "
            "(2) share the url as a clickable markdown link, describing it as the "
            "place to review/edit the generated data on the Plurai platform; "
            "(3) call evals_ask_user with the optimization-type question — options "
            '"SLM — recommended for production, fine-tuned model (~20 min)" '
            '(value "SLM") and "LLM — recommended for testing/small scale, '
            'prompt-based (~2 min)" (value "LLM"). '
            "Do NOT add any extra confirmation question before the optimization ask."
        )
    return result


# ── ask_user (elicitation) ───────────────────────────────────────────────
async def _ask_user(args: AskUserArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    state = _state_of(ctx)
    # Single gate: only allow ask_user right after start_judge or a
    # send_message that re-armed has_questions. Stops the model from
    # injecting its own questions before the flow has begun.
    if not state.has_questions:
        return {
            "error": (
                "You must call evals_start_judge first. "
                "Do NOT ask your own questions before the flow has begun."
            )
        }
    state.has_questions = False
    questions = args.questions

    form_model = _build_elicit_form(questions)
    try:
        result = await ctx.elicit(message="Please answer these questions:", schema=form_model)
    except McpError:
        # Host doesn't implement elicitation. Fall back.
        return _fallback_payload(questions, state)
    except Exception:
        # Unknown failure (transport, schema bug, internal). Log so we can
        # debug in environments that should support elicitation, and still
        # fall back so the user isn't stuck.
        logger.exception(
            "Elicitation failed unexpectedly; falling back",
            question_count=len(questions),
        )
        return _fallback_payload(questions, state)
    # Decline / cancel both fall through to the AskUserQuestion fallback so
    # the user isn't stranded — same shape as elicit-unavailable.
    if not isinstance(result, AcceptedElicitation):
        return _fallback_payload(questions, state)
    data_dict = result.data.model_dump()
    answers = {q.question: data_dict.get(f"q{i + 1}", "") for i, q in enumerate(questions)}
    return {"answers": answers, "action": "accepted"}


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
