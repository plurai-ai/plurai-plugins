"""Evaluator-flow tools: start_evaluator, send_message, ask_user.

`evals_send_message` includes a fast-path that short-circuits a duplicate
optimize when the classifier has already-completed or in-progress results.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict, cast

import httpx
import structlog
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from ..clients import AgentEvent, GetClassifierResponse, OptimizationView
from ..config import Settings, get_settings
from ..errors import format_tool_error
from ..state import ServerState

logger: Any = structlog.get_logger(__name__)

# ── Input models ──────────────────────────────────────────────────────────

_StrictModel = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StartEvaluatorArgs(BaseModel):
    model_config = _StrictModel
    task_description: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1024,
            description=(
                "Several sentences, max 1024 chars. Include task + desired label names. "
                "No examples or criteria. "
                "Example: 'Classify responses as health_advice or safe.'"
            ),
        ),
    ]


class SendMessageArgs(BaseModel):
    model_config = _StrictModel
    thread_id: Annotated[
        str, Field(min_length=1, description="Thread ID returned by evals_start_evaluator.")
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


async def _start_optimize_and_await_classifier(
    state: ServerState, thread_id: str, message: str, timeout: float
) -> str | None:
    """Fire the agent's optimize run as a background task and await the
    classifier_id appearing in a STATE_SNAPSHOT event mid-stream.

    The SSE stream stays open for the full optimization (~20 min for SLM),
    so we can't synchronously await ``run_agent``. The background task
    keeps consuming events; an ``asyncio.Event`` signals the foreground as
    soon as ``classifier_id`` lands. Returns the classifier_id, or None if
    it didn't surface within ``timeout``.
    """
    classifier_seen = asyncio.Event()
    captured_id: str | None = None

    def on_event(event: dict[str, Any]) -> None:
        nonlocal captured_id
        if event.get("type") != "STATE_SNAPSHOT":
            return
        snapshot = event.get("snapshot")
        if not isinstance(snapshot, dict):
            return
        cid = cast(dict[str, Any], snapshot).get("classifier_id")
        if isinstance(cid, str) and cid:
            captured_id = cid
            classifier_seen.set()

    async def run_optimize() -> None:
        try:
            # timeout=None disables the per-event read timeout. The optimize
            # SSE stream is intrinsically long-lived (~20 min for SLM) with
            # potentially long gaps during model training; a per-event
            # ceiling would kill legitimate runs without protecting against
            # anything we care about. Background task lifecycle is bounded
            # by state.background_tasks drain on lifespan shutdown.
            await state.agent.run_agent(thread_id, message, timeout=None, on_event=on_event)
        except (httpx.HTTPError, OSError):
            logger.exception(
                "Background optimize failed",
                thread_id=thread_id,
                message=message[:120],
            )

    task = asyncio.create_task(run_optimize())
    state.background_tasks.add(task)
    task.add_done_callback(state.background_tasks.discard)

    try:
        await asyncio.wait_for(classifier_seen.wait(), timeout=timeout)
    except TimeoutError:
        logger.warning(
            "Classifier ID did not surface in time",
            thread_id=thread_id,
            message=message[:120],
            timeout=timeout,
        )
        return None
    logger.info("Classifier ID surfaced", classifier_id=captured_id)
    return captured_id


# ── Optimization fast-path (pre-send check) ──────────────────────────────


async def _check_optimization_status(
    state: ServerState, classifier_id: str, slug: str, version: str
) -> dict[str, Any] | None:
    """If a classifier already has results / is mid-optimization, return a
    short-circuit response. Returning None means the caller should send the
    message to the agent normally."""
    settings = get_settings()

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


# How long to wait for the agent's STATE_SNAPSHOT to surface the new
# classifier_id after firing the optimize agent run. Classifier creation
# happens in the first few seconds of the run; 60s is generous.
_CLASSIFIER_WAIT_TIMEOUT_S = 60.0


async def _handle_optimize(
    state: ServerState, settings: Settings, thread_id: str, message: str
) -> dict[str, Any]:
    """Optimize fast-path.

    The classifier is created *during* the agent run, and the SLM run keeps
    the SSE stream open for the full ~20 min. So
    :func:`_start_optimize_and_await_classifier` fires the run as a
    background task and resolves once a STATE_SNAPSHOT carrying
    ``classifier_id`` lands. We then resolve slug/version, run the
    already-optimized short-circuit, and return a self-sufficient payload
    that echoes every ID the orchestrator needs.

    If classifier_id doesn't surface in time (rare), return
    ``optimization_started_pending_id`` so the orchestrator can recover
    via re-scheduling — never guess an ID. Wait-time guidance lives in the
    skill (it already told the user how long SLM/LLM take).
    """
    classifier_id = await _start_optimize_and_await_classifier(
        state, thread_id, message, _CLASSIFIER_WAIT_TIMEOUT_S
    )
    if classifier_id is None:
        # The optimize SSE stream opened, but no STATE_SNAPSHOT carrying
        # classifier_id arrived within the wait budget. Without an ID
        # there's no programmatic recovery — raise so the orchestrator
        # gets a clean error envelope and can retry from scratch.
        raise RuntimeError(
            f"Optimize triggered for thread {thread_id} but no classifier_id "
            f"emitted within {_CLASSIFIER_WAIT_TIMEOUT_S:.0f}s — "
            f"check {settings.api_base.rstrip('/')}/thread/{thread_id} and retry."
        )

    classifier: GetClassifierResponse = await state.platform.get_classifier(classifier_id)
    slug = classifier.slug
    version = classifier.default_version.number if classifier.default_version else "1.0.0"

    status = await _check_optimization_status(state, classifier_id, slug, version)
    if status:
        return status

    return {
        "status": "optimization_started",
        "message": (
            f"Optimization '{message}' triggered for thread {thread_id}. "
            "It runs in the background (~2 min for LLM, ~20 min for SLM)."
        ),
        "classifier_id": classifier_id,
        "slug": slug,
        "version": version,
        "endpoint_url": f"{settings.run_url}/ioa/v1/{slug}/{version}",
        "thread_id": thread_id,
        "url": f"{settings.api_base.rstrip('/')}/thread/{thread_id}",
    }


# ── Tool implementations ─────────────────────────────────────────────────


async def _start_evaluator(args: StartEvaluatorArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    state = _state_of(ctx)
    settings = get_settings()

    thread = await state.platform.create_thread()

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

    # Re-arm so the model can call evals_ask_user next, whether the agent
    # came back with another refinement question or just finished the
    # initial flow and the next step is the SLM/LLM choice.
    state.has_questions = True

    if state.commit_id is not None:
        result["url"] = f"{settings.api_base.rstrip('/')}/thread/{thread_id}"
        result["instructions"] = (
            "The synthetic examples are ready. You MUST do all three in this turn: "
            "(1) show the user the agent_response text verbatim; "
            "(2) share the url as a clickable markdown link whose link text is "
            "exactly 'UI experience' — do NOT substitute any other label such "
            "as 'Data Canvas', the evaluator name, or the thread title. After "
            "the link, tell the user they can review/edit the generated data "
            "and also track progress or generate more evals in the UI "
            "experience; "
            "(3) call evals_ask_user with the model-choice question. Use header "
            '"Model Choice" and question "Which model would you like to generate?". '
            "Options: "
            '"SLM - best for production scale (recommended)" (value "SLM") with '
            'description "Our fine-tuned small-language model with low inference '
            "cost, realtime latency, and high accuracy. Pro plan only. "
            '~20 min."; and '
            '"Optimized LLM - for dev iterations" (value "LLM") with description '
            '"Our calibration on a large language model, best for local checks '
            'and quick validations. ~2 min.". '
            "Do NOT add an explicit Other option, and do NOT add any extra "
            "confirmation question before this ask."
        )
    return result


# ── ask_user ─────────────────────────────────────────────────────────────
async def _ask_user(args: AskUserArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    """Returns a payload that instructs the model to call the host's
    ``AskUserQuestion`` tool. We don't use MCP elicitation: it can't render
    per-option descriptions or headers, so it would show a strictly poorer
    prompt than ``AskUserQuestion`` provides.
    """
    state = _state_of(ctx)
    # Single gate: only allow ask_user right after a tool that armed
    # has_questions — start_evaluator, a send_message that re-armed it, or a
    # search_evaluators that returned matches. Stops the model from injecting
    # its own questions before any flow has begun.
    if not state.has_questions:
        return {
            "error": (
                "You must call evals_start_evaluator or evals_search_evaluators first. "
                "Do NOT ask your own questions before the flow has begun."
            )
        }
    state.has_questions = False

    extra = ""
    if state.commit_id is not None:
        # Post-initial-flow, the most consequential question is SLM-vs-LLM,
        # where the follow-up send_message must use EXACT formatting. The
        # conditional framing makes this a no-op for other questions (e.g.
        # the integration-language picker).
        extra = (
            " IF this is the model-choice question (SLM vs LLM), then after the user "
            "chooses, call evals_send_message with EXACTLY message='Optimize [LLM]' or "
            "message='Optimize [SLM]'. One call only. These are hardcoded strings — do not "
            "modify them."
        )
    return {
        "action": "ask_user_question",
        "instructions": (
            "Call the AskUserQuestion tool with the questions below. "
            "Use ToolSearch to load it first if needed. Do NOT answer the questions yourself."
            + extra
        ),
        "askUserQuestions": [
            {
                "question": q.question,
                "header": q.question[:12],
                "options": [{"label": o.label, "description": o.value} for o in q.options],
                "multiSelect": False,
            }
            for q in args.questions
        ],
    }


# ── Registration ─────────────────────────────────────────────────────────


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="evals_start_evaluator",
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
    async def evals_start_evaluator(
        args: StartEvaluatorArgs, ctx: Context[Any, Any, Any]
    ) -> dict[str, Any]:
        try:
            return await _start_evaluator(args, ctx)
        except (httpx.HTTPStatusError, httpx.TransportError, RuntimeError) as e:
            return format_tool_error(e)

    @mcp.tool(
        name="evals_send_message",
        description=(
            "Send a follow-up message to the Plurai agent. Only use AFTER evals_start_evaluator. "
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
    _ = (evals_start_evaluator, evals_send_message, evals_ask_user, Literal)
