"""Evaluator-flow tools: start_evaluator, send_message, ask_user."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, cast

import httpx
import structlog
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..clients import GetClassifierResponse, ThreadStateView
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


# ── Helpers ──────────────────────────────────────────────────────────────


def _state_of(ctx: Context[Any, Any, Any]) -> ServerState:
    return cast(ServerState, ctx.request_context.lifespan_context)


async def _start_optimize_and_await_classifier(
    state: ServerState, thread_id: str, message: str, timeout: float
) -> str | None:
    """Fire the agent's optimize run as a background task and await the
    classifier_id appearing in an intermediate-state event mid-stream.

    The run stream stays open for the full optimization (~20 min for SLM),
    so we can't synchronously await ``run_agent``. The background task
    keeps consuming events; an ``asyncio.Event`` signals the foreground as
    soon as ``classifier_id`` lands. Returns the classifier_id, or None if
    it didn't surface within ``timeout``.
    """
    classifier_seen = asyncio.Event()
    captured_id: str | None = None
    captured_error: BaseException | None = None

    def on_state(snapshot: ThreadStateView) -> None:
        nonlocal captured_id
        if snapshot.classifier_id:
            captured_id = snapshot.classifier_id
            classifier_seen.set()

    async def run_optimize() -> None:
        # Failures that happen before classifier_id surfaces are captured
        # and re-raised by the foreground so the tool wrapper can map them
        # (e.g. MissingApiKeyError → inline auth prompt) instead of letting
        # the foreground time out with a misleading "no classifier_id" error.
        nonlocal captured_error
        try:
            await state.agent.run_agent(thread_id, message, on_state=on_state)
        except (httpx.HTTPError, OSError, RuntimeError, ValidationError) as e:
            captured_error = e
            logger.exception(
                "Background optimize failed",
                thread_id=thread_id,
                message=message[:120],
                classifier_already_surfaced=captured_id is not None,
            )
        finally:
            # Unblock the foreground in every termination case so a stream
            # that ends without emitting classifier_id (legitimate completion
            # or error) doesn't burn the full wait budget.
            classifier_seen.set()

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
    if captured_id is not None:
        logger.info("Classifier ID surfaced", classifier_id=captured_id)
        return captured_id
    if captured_error is not None:
        # Background failed before classifier_id surfaced — re-raise so the
        # tool wrapper formats it (auth prompts, 401s, transport errors)
        # instead of the foreground returning None and producing a generic
        # "no classifier_id emitted" timeout.
        raise captured_error
    return None


# ── Optimize handler ─────────────────────────────────────────────────────


async def _handle_optimize(
    state: ServerState, settings: Settings, thread_id: str, message: str
) -> dict[str, Any]:
    """Optimize fast-path.

    The classifier is created *during* the agent run, and the SLM run keeps
    the run stream open for the full ~20 min. So
    :func:`_start_optimize_and_await_classifier` fires the run as a
    background task and resolves once an intermediate-state event carrying
    ``classifier_id`` lands. We then resolve slug/version and return a
    self-sufficient payload that echoes every ID the orchestrator needs.

    If classifier_id doesn't surface within ``classifier_wait_timeout_s``
    (rare), raise ``RuntimeError`` so the orchestrator gets a clean error
    envelope and can retry from scratch — never guess an ID.
    """
    classifier_id = await _start_optimize_and_await_classifier(
        state, thread_id, message, settings.classifier_wait_timeout_s
    )
    if classifier_id is None:
        # The optimize stream opened, but no intermediate-state event
        # carrying classifier_id arrived within the wait budget. Without
        # an ID there's no programmatic recovery — raise so the
        # orchestrator gets a clean error envelope and can retry from scratch.
        raise RuntimeError(
            f"Optimize triggered for thread {thread_id} but no classifier_id "
            f"emitted within {settings.classifier_wait_timeout_s:.0f}s — "
            f"check {settings.api_base.rstrip('/')}/thread/{thread_id} and retry."
        )

    classifier: GetClassifierResponse = await state.platform.get_classifier(classifier_id)
    slug = classifier.slug
    version = classifier.default_version.number if classifier.default_version else "1.0.0"

    return {
        "status": "optimization_started",
        "message": (
            f"Optimization '{message}' triggered for thread {thread_id}. "
            "It runs in the background (~2 min for LLM, ~20 min for SLM)."
        ),
        "classifier_id": classifier_id,
        "slug": slug,
        "version": version,
        "endpoint_url": f"{settings.run_base}/ioa/v1/{slug}/{version}",
        "thread_id": thread_id,
        "url": f"{settings.api_base.rstrip('/')}/thread/{thread_id}",
    }


# ── Tool implementations ─────────────────────────────────────────────────


async def _start_evaluator(args: StartEvaluatorArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    state = _state_of(ctx)
    settings = get_settings()

    thread = await state.platform.create_thread()

    await state.agent.run_agent(thread.id, args.task_description)
    snapshot = await state.agent.get_state(thread.id)
    agent_response = snapshot.last_assistant_message()
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

    await state.agent.run_agent(thread_id, message)
    snapshot = await state.agent.get_state(thread_id)
    state.committed = snapshot.commit_id is not None

    result: dict[str, Any] = {
        "agent_response": snapshot.last_assistant_message(),
        "message_count": len(snapshot.messages),
    }
    if snapshot.classifier_id:
        result["classifier_id"] = snapshot.classifier_id

    # Re-arm so the model can call evals_ask_user next, whether the agent
    # came back with another refinement question or just finished the
    # initial flow and the next step is the SLM/LLM choice.
    state.has_questions = True

    if state.committed:
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
    if state.committed:
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
        except (
            httpx.HTTPStatusError,
            httpx.TransportError,
            RuntimeError,
            ValidationError,
            ValueError,
        ) as e:
            logger.exception("evals_start_evaluator failed")
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
        except (
            httpx.HTTPStatusError,
            httpx.TransportError,
            RuntimeError,
            ValidationError,
            ValueError,
        ) as e:
            logger.exception("evals_send_message failed")
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
    _ = (evals_start_evaluator, evals_send_message, evals_ask_user)
