"""Evaluator-flow tools: start_evaluator, send_message, ask_user."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import Annotated, Any, cast

import httpx
import structlog
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..clients import GetClassifierResponse, ThreadStateView
from ..config import Settings, get_settings
from ..errors import CorruptCredentialsError, MissingApiKeyError, format_tool_error
from ..state import OptimizeRun, ServerState

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
                "1-2 short sentences, max 1024 chars. Describe the core task and "
                "include desired label names only if the user already mentioned them — "
                "do NOT pre-ask the user for labels; the refinement round covers them. "
                "No examples or criteria. "
                "Example: 'Classify responses as health_advice or safe.'"
            ),
        ),
    ]


class SendMessageArgs(BaseModel):
    model_config = _StrictModel
    thread_id: Annotated[
        str, Field(min_length=1, description="Thread ID returned by start_evaluator.")
    ]
    message: Annotated[str, Field(min_length=1, description="Message to send to the Plurai agent.")]


class AskUserOption(BaseModel):
    model_config = _StrictModel
    label: Annotated[str, Field(min_length=1, description="Display text for the option.")]
    description: Annotated[
        str,
        Field(
            min_length=1,
            description="Explanation of what this option means or what happens if chosen.",
        ),
    ]


class AskUserQuestion(BaseModel):
    model_config = _StrictModel
    question: Annotated[str, Field(min_length=1, description="The question text.")]
    options: Annotated[
        list[AskUserOption],
        Field(
            min_length=2,
            max_length=4,
            description=(
                "Selectable options for this question (2-4). If only one viable "
                "option remains, do NOT call this tool — proceed directly instead."
            ),
        ),
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


async def _check_slm_entitlement(state: ServerState) -> bool:
    """Return whether the authenticated org may run SLM optimization.

    Fails closed: any non-auth error from ``GET /plan`` (network, 5xx,
    validation) is logged at warning level and treated as "no entitlement".
    Auth errors (missing/corrupt credentials or HTTP 401) propagate so the
    existing inline auth flow can fire.
    """
    try:
        plan = await state.platform.get_plan()
    except (MissingApiKeyError, CorruptCredentialsError):
        raise
    except httpx.HTTPStatusError as e:
        if e.response.status_code == HTTPStatus.UNAUTHORIZED:
            raise
        logger.warning(
            "Plan fetch failed; failing closed on SLM entitlement",
            status_code=e.response.status_code,
        )
        return False
    except (httpx.TransportError, ValidationError, RuntimeError) as e:
        logger.warning("Plan fetch failed; failing closed on SLM entitlement", error=str(e))
        return False
    return plan.entitlements.slm_endpoints


async def _start_optimize_and_await_classifier(
    state: ServerState, thread_id: str, message: str, timeout: float
) -> str | None:
    """Fire the agent's optimize run as a background task (at most once per
    thread, ever) and await the classifier_id appearing in an
    intermediate-state event mid-stream.

    The run stream stays open for the full optimization (~20 min for SLM),
    so we can't synchronously await ``run_agent``. The background task
    keeps consuming events; an ``asyncio.Event`` signals the foreground as
    soon as ``classifier_id`` lands.

    Idempotency: subsequent calls with the same ``thread_id`` resume the
    existing ``OptimizeRun`` instead of starting a second agent run — that
    re-fire is what would otherwise cause two parallel background runs on the
    same thread (which can happen when batch optimizes time out and the
    orchestrator retries). Returns the classifier_id once captured, or
    ``None`` if it hasn't surfaced yet (timeout) or if the agent run ended
    without ever emitting one (degenerate). A ``captured_error`` on the
    existing run is re-raised so the tool wrapper can format it.
    """
    existing = state.optimize_runs.get(thread_id)
    if existing is not None:
        # A run that terminated with an error *before* a classifier_id ever
        # surfaced protects no server-side classifier, and the cause may be
        # transient (5xx) or fixable (the user re-authed after a
        # MissingApiKeyError). Drop the dead entry so this explicit retry
        # starts a fresh run instead of replaying the cached error forever —
        # no retry could ever clear it otherwise. A run that already emitted a
        # classifier_id is never dropped: that id is the durable handoff, so
        # we resume it (returning the id) even if the run later failed.
        if (
            existing.task.done()
            and existing.captured_error is not None
            and existing.captured_id is None
        ):
            state.optimize_runs.pop(thread_id, None)
            state.background_tasks.discard(existing.task)
        else:
            return await _await_optimize_run(existing, thread_id, message, timeout)

    event = asyncio.Event()

    def on_state(snapshot: ThreadStateView) -> None:
        if snapshot.classifier_id and run.captured_id is None:
            run.captured_id = snapshot.classifier_id
            run.event.set()

    async def run_optimize() -> None:
        # Failures that happen before classifier_id surfaces are captured
        # and re-raised by the foreground so the tool wrapper can map them
        # (e.g. MissingApiKeyError → inline auth prompt) instead of letting
        # the foreground time out with a misleading "no classifier_id" error.
        try:
            await state.agent.run_agent(thread_id, message, on_state=on_state)
        except (httpx.HTTPError, OSError, RuntimeError, ValidationError) as e:
            run.captured_error = e
            logger.exception(
                "Background optimize failed",
                thread_id=thread_id,
                message=message[:120],
                classifier_already_surfaced=run.captured_id is not None,
            )
        finally:
            # Unblock all current and future waiters in every termination
            # case so a stream that ends without emitting classifier_id
            # (legitimate completion or error) doesn't burn the wait budget.
            run.event.set()

    # Create the task before the record so ``run.task`` is a live Task from
    # construction — never a placeholder. ``on_state``/``run_optimize`` close
    # over ``run`` by name (late binding); create_task only *schedules* the
    # coroutine, so its body first runs at the await below, by which point
    # ``run`` is already bound.
    task = asyncio.create_task(run_optimize())
    run = OptimizeRun(task=task, event=event)
    state.optimize_runs[thread_id] = run
    state.background_tasks.add(task)
    task.add_done_callback(state.background_tasks.discard)

    return await _await_optimize_run(run, thread_id, message, timeout)


async def _await_optimize_run(
    run: OptimizeRun, thread_id: str, message: str, timeout: float
) -> str | None:
    """Wait on an existing ``OptimizeRun`` for up to ``timeout`` seconds.

    Returns the captured classifier_id if present; ``None`` if the run is
    still in flight (caller surfaces resumable-timeout) or if it ended
    cleanly without ever emitting one (caller surfaces hard failure).
    Re-raises ``captured_error`` if the run terminated with one.
    """
    if run.captured_id is not None:
        return run.captured_id
    if run.captured_error is not None:
        raise run.captured_error
    try:
        await asyncio.wait_for(run.event.wait(), timeout=timeout)
    except TimeoutError:
        logger.warning(
            "Classifier ID did not surface in time",
            thread_id=thread_id,
            message=message[:120],
            timeout=timeout,
        )
        return None
    if run.captured_id is not None:
        logger.info("Classifier ID surfaced", classifier_id=run.captured_id)
        return run.captured_id
    if run.captured_error is not None:
        raise run.captured_error
    return None


# ── Optimize handler ─────────────────────────────────────────────────────


async def _handle_optimize(
    state: ServerState, settings: Settings, thread_id: str, message: str
) -> dict[str, Any]:
    """Handle an ``Optimize [LLM]``/``[SLM]`` message: validate, gate, fire
    the background run, and await the classifier_id.

    The classifier is created *during* the agent run, and the SLM run keeps
    the run stream open for the full ~20 min. So
    :func:`_start_optimize_and_await_classifier` fires the run as a
    background task (exactly once per thread) and resolves once an
    intermediate-state event carrying ``classifier_id`` lands. We then
    resolve slug/version and return a self-sufficient payload that echoes
    every ID the orchestrator needs.

    Two terminal None cases need to be distinguished:

    - **Still in flight after timeout** (common under batch load): return a
      resumable error envelope telling the orchestrator to wake up later and
      re-call with the same message — `_start_optimize_and_await_classifier`
      enforces one-run-per-thread, so the resume re-awaits the same task
      rather than firing a duplicate.
    - **Agent run already terminated without emitting classifier_id**
      (degenerate): raise ``RuntimeError``. The one-run rule precludes
      restarting, so this is a hard failure that the orchestrator surfaces.

    Backstops (both defend against orchestrator drift):

    1. **Format**: reject any ``Optimize [X]`` whose payload isn't ``[SLM]``
       or ``[LLM]``. The model-choice ask only ever yields those two; a
       malformed payload means the orchestrator interpreted an "Other" /
       declined ask into something we don't accept. The Plurai agent would
       silently mishandle it, so we fail loudly here and route the
       orchestrator back to ``ask_user``.
    2. **Entitlement**: if the org lacks SLM entitlement, reject
       ``Optimize [SLM]`` before starting the background run.
       ``state.slm_allowed`` is refreshed from ``GET /plan`` only on the most
       recent *committed* ``_send_message`` and defaults to True, so this
       backstop bites only post-commit — an ``Optimize [SLM]`` arriving before
       any commit (pure orchestrator drift) is not gated here, but that's the
       same window the gated UX itself hasn't run in. The gated UX in
       ``_send_message`` is the primary control; this is the post-commit
       backstop against drift.
    """
    normalized = message.strip().lower()
    if normalized not in ("optimize [slm]", "optimize [llm]"):
        return {
            "error": (
                f"Invalid optimize message {message!r}. Expected exactly "
                "'Optimize [LLM]' or 'Optimize [SLM]' (square brackets are "
                "literal). The user must explicitly pick a model via the "
                "Model Choice ask_user — never invent a payload from "
                "an 'Other' / declined answer. Re-ask via ask_user "
                "with the two model options per the eval skill / command docs."
            )
        }
    if normalized == "optimize [slm]" and not state.slm_allowed:
        return {
            "error": (
                "SLM vibe-training requires a paid Plurai plan. Upgrade at "
                f"{settings.api_base.rstrip('/')}/settings?tab=subscription-billing, "
                "then retry. To run now, pick the Optimized LLM option instead."
            ),
            "recovery_hint": (
                "Re-ask the model choice via ask_user using the upgrade-gated "
                "variant from the eval skill. If the user picks 'Continue with "
                "Optimized LLM', send 'Optimize [LLM]'."
            ),
        }
    classifier_id = await _start_optimize_and_await_classifier(
        state, thread_id, message, settings.classifier_wait_timeout_s
    )
    if classifier_id is None:
        run = state.optimize_runs.get(thread_id)
        if run is not None and not run.task.done():
            return {
                "error": (
                    f"Optimize is still running in the background for thread "
                    f"{thread_id} — classifier_id hasn't surfaced yet (common "
                    f"under batch load when several optimizes run in parallel). "
                    f"The server enforces ONE optimize run per thread, so this "
                    f"run is the run — do NOT start a fresh evaluator and do "
                    f"NOT change the message. Schedule a wake-up of ~120s, "
                    f"then call send_message(thread_id, '{message}') "
                    f"again with the SAME message — it re-awaits this "
                    f"existing run, never restarts. "
                    f"Check {settings.api_base.rstrip('/')}/thread/{thread_id} "
                    f"for progress."
                )
            }
        # Run terminated without emitting classifier_id. Under the one-run
        # rule we won't restart it — surface as a hard failure.
        raise RuntimeError(
            f"Optimize for thread {thread_id} terminated without emitting a "
            f"classifier_id — check "
            f"{settings.api_base.rstrip('/')}/thread/{thread_id}. "
            f"This thread cannot be re-optimized; start a new evaluator if "
            f"you need to retry."
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
        "instructions": (
            "Optimization is now running in the background. Schedule a "
            "wake-up via ScheduleWakeup (delaySeconds=120 for LLM, 1200 "
            "for SLM) and END this turn. Do NOT call send_message, "
            "ask_user, or any other tool. The only legitimate next "
            "tool call is get_results on wake-up — pass the "
            "classifier_id from this response."
        ),
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
    # Reset persistent state — a prior evaluator on this server may have
    # committed, and the leaked True would mis-route the post-commit branch
    # of _send_message on the very first follow-up.
    state.committed = False
    state.slm_allowed = True

    return {
        "thread_id": thread.id,
        "example_set_id": thread.example_set_id,
        "url": f"{settings.api_base.rstrip('/')}/thread/{thread.id}",
        "agent_response": agent_response,
        "platform_constraint": (
            "TASK DEFINITION IS FROZEN. The task_description passed here is "
            "permanent for this evaluator — subsequent send_message calls "
            "only refine the generated samples, never the task itself "
            "(judging criteria, scope). If the user later wants to change the "
            "task, you MUST start a fresh evaluator by calling "
            "start_evaluator again with a revised task_description. "
            "Never try to amend the task via send_message."
        ),
        "instructions": (
            "Refinement questions in agent_response cover the OVERALL task "
            "(labels, scope, criteria) — never per-example. If the user "
            "handed you a spec source to act on, self-answer them via "
            "send_message from that source. Otherwise, route them "
            "to the user via ask_user, rephrased as options. When "
            "ambiguous, ask. User-facing questions always go through "
            "ask_user — never plain text."
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

    # Optimization-in-flight guard: once a thread has fired Optimize and the
    # background agent run hasn't finished, the orchestrator's only legitimate
    # action is polling get_results. Stray send_message calls during
    # this window (~2 min LLM, ~20 min SLM) are off-task and land on the live
    # thread, derailing optimization. Reject them with a recovery hint that
    # routes the orchestrator back to the poll.
    opt = state.optimize_runs.get(thread_id)
    if opt is not None and not opt.task.done():
        return {
            "error": (
                f"Optimization is in progress for thread {thread_id} "
                f"(classifier_id {opt.captured_id or 'pending'}). Do not "
                "send messages until results land. Schedule a wake-up via "
                "ScheduleWakeup and call get_results on wake-up."
            ),
            "recovery_hint": "get_results",
        }

    await state.agent.run_agent(thread_id, message)
    snapshot = await state.agent.get_state(thread_id)
    state.committed = snapshot.commit_id is not None

    result: dict[str, Any] = {
        "agent_response": snapshot.last_assistant_message(),
        "message_count": len(snapshot.messages),
    }
    if snapshot.classifier_id:
        result["classifier_id"] = snapshot.classifier_id

    # Re-arm so the model can call ask_user next, whether the agent
    # came back with another refinement question or just finished the
    # initial flow and the next step is the SLM/LLM choice.
    state.has_questions = True

    if state.committed:
        state.slm_allowed = await _check_slm_entitlement(state)
        result["url"] = f"{settings.api_base.rstrip('/')}/thread/{thread_id}"
        result["slm_allowed"] = state.slm_allowed
        result["instructions"] = (
            "The synthetic examples are ready. Show the user agent_response "
            "verbatim, share url as a 'UI experience' markdown link (exact link "
            "text — not 'Data Canvas' or the thread title) with a one-line note "
            "that they can review/edit data and track progress there, then call "
            "ask_user for the Model Choice question per the eval skill / "
            "command docs. Apply the slm_allowed gate from those docs — when "
            "false, swap the SLM option for a 'Wait — upgrade plan first' "
            "option so the ask still has 2+ options."
        )
        result["platform_constraint"] = (
            "TASK DEFINITION IS FROZEN. If the user reacts to the samples by "
            "asking to change the judging criteria or task scope, do NOT "
            "send that as a chat message — send_message can only refine "
            "samples, not the task. Tell the user the task can't be edited, "
            "confirm they want to restart, then call start_evaluator "
            "with a revised task_description. For sample edits "
            "(add/remove/modify individual examples), prefer the UI experience "
            "link surfaced above; only edit via send_message if the user "
            "explicitly asks to do it through chat."
        )
    else:
        result["instructions"] = (
            "If agent_response is another refinement question, apply the "
            "same routing rule from start_evaluator (delegated → "
            "answer yourself; specific → ask_user; ambiguous → "
            "ask the user). If it's a status update, surface it verbatim "
            "and wait — no follow-up tool call required."
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
                "You must call start_evaluator or search_evaluators first. "
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
            " IF this is the model-choice question: after the user picks, call "
            "send_message ONCE with EXACTLY 'Optimize [LLM]' or "
            "'Optimize [SLM]' (hardcoded — do not modify). On Other / decline / "
            "ambiguity, follow the Other policy in the eval skill / command — "
            "never fire optimization without an explicit user pick."
        )
    return {
        "action": "ask_user_question",
        "instructions": (
            "Call the AskUserQuestion tool with the questions below. "
            "Use ToolSearch to load it first if needed. Do NOT answer the questions yourself."
            + extra
            + " If AskUserQuestion returns 'User declined to answer questions' (or is "
            "otherwise cancelled/interrupted/escaped), treat it as a SKIP and follow "
            "the default action documented in your skill or command for this specific "
            "decision point. Do NOT retry the same ask, do NOT re-call ask_user "
            "with the same questions, do NOT surface a 'tool interrupted' or 'user "
            "declined' message verbatim to the user, and do NOT stall the flow."
        ),
        "askUserQuestions": [
            {
                "question": q.question,
                "header": q.question[:12],
                "options": [{"label": o.label, "description": o.description} for o in q.options],
                "multiSelect": False,
            }
            for q in args.questions
        ],
    }


# ── Wrapper error envelopes ──────────────────────────────────────────────
# Extracted from the @mcp.tool wrappers so unit tests can pin the envelope
# shape without spinning up FastMCP. The hints exist to stop the orchestrator
# from "restarting from start_evaluator" on a transient 5xx and re-firing the
# whole tool flow.


def _send_message_error_envelope(thread_id: str, exc: BaseException) -> dict[str, Any]:
    envelope: dict[str, Any] = dict(format_tool_error(exc))
    envelope["thread_id"] = thread_id
    envelope["recovery_hint"] = (
        "Transient failure — the thread is still alive on the platform. "
        "Surface the error to the user and ask whether to retry. On yes, "
        "call send_message AGAIN with the SAME thread_id and message. "
        "Do NOT call start_evaluator (would create a new thread, "
        "orphaning this one) or search_evaluators (restarts the flow "
        "from the top and re-fires every subsequent tool)."
    )
    return envelope


def _start_evaluator_error_envelope(exc: BaseException) -> dict[str, Any]:
    envelope: dict[str, Any] = dict(format_tool_error(exc))
    envelope["recovery_hint"] = (
        "Surface the error to the user and ask whether to retry. On yes, "
        "call start_evaluator again with the SAME task_description. "
        "Do NOT loop back through search_evaluators — the user "
        "already chose to create new."
    )
    return envelope


# ── Registration ─────────────────────────────────────────────────────────


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="start_evaluator",
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
    async def start_evaluator(
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
            logger.exception("start_evaluator failed")
            return _start_evaluator_error_envelope(e)

    @mcp.tool(
        name="send_message",
        description=(
            "Send a follow-up message to the Plurai agent. Only use AFTER start_evaluator. "
            "Used for sending user answers and for triggering optimization. "
            "To trigger optimization, send EXACTLY 'Optimize [LLM]' or 'Optimize [SLM]' "
            "(square brackets are literal). Optimization runs in the background — "
            "call get_results to retrieve results."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def send_message(args: SendMessageArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
        try:
            return await _send_message(args, ctx)
        except (
            httpx.HTTPStatusError,
            httpx.TransportError,
            RuntimeError,
            ValidationError,
            ValueError,
        ) as e:
            logger.exception("send_message failed")
            return _send_message_error_envelope(args.thread_id, e)

    @mcp.tool(
        name="ask_user",
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
    async def ask_user(args: AskUserArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
        return await _ask_user(args, ctx)

    # Avoid unused-name warnings under strict linters.
    _ = (start_evaluator, send_message, ask_user)
