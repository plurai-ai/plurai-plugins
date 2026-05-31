"""Classifier tools: search_evaluators, get_results, get_api_key."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal, cast

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from ..auth import load_api_key
from ..clients import (
    ClassifierSummaryView,
    GetClassifierResponse,
    MetricsView,
    OptimizationView,
)
from ..config import get_settings
from ..errors import MissingApiKeyError, format_tool_error
from ..state import ServerState

_StrictModel = ConfigDict(extra="forbid", str_strip_whitespace=True)
ResponseFormat = Literal["json", "markdown"]


# ── Input models ──────────────────────────────────────────────────────────


class SearchEvaluatorsArgs(BaseModel):
    model_config = _StrictModel
    limit: Annotated[int, Field(default=25, ge=1, le=100, description="Max results per page.")]
    offset: Annotated[int, Field(default=0, ge=0, description="Number of results to skip.")]
    response_format: Annotated[
        ResponseFormat,
        Field(
            default="markdown",
            description="'markdown' for human display, 'json' for machine-readable.",
        ),
    ]


class GetResultsArgs(BaseModel):
    model_config = _StrictModel
    classifier_id: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Classifier ID from the prior evals_send_message Optimize response. "
                "The MCP server is stateless across subprocess restarts; the "
                "orchestrator's conversation context is the durable handoff."
            ),
        ),
    ]
    response_format: Annotated[
        ResponseFormat,
        Field(
            default="markdown",
            description="'markdown' for human display, 'json' for machine-readable.",
        ),
    ]


class GetApiKeyArgs(BaseModel):
    model_config = _StrictModel


# ── Helpers ──────────────────────────────────────────────────────────────


def _state(ctx: Context[Any, Any, Any]) -> ServerState:
    return cast(ServerState, ctx.request_context.lifespan_context)


async def _has_optimization(
    state: ServerState, classifier_uuid: str, slug: str, version: str
) -> bool:
    for identifier in (classifier_uuid, slug):
        if (await state.platform.get_optimization(identifier, version)) is not None:
            return True
    return False


async def _fetch_optimization(
    state: ServerState, classifier_uuid: str, slug: str, version: str
) -> OptimizationView | None:
    for identifier in (classifier_uuid, slug):
        opt = await state.platform.get_optimization(identifier, version)
        if opt is not None:
            return opt
    return None


def _labels_of(c: ClassifierSummaryView) -> list[str]:
    properties = c.output_schema.get("properties", {})
    if not isinstance(properties, dict):
        return []
    label_props = cast(dict[str, Any], properties).get("label", {})
    if not isinstance(label_props, dict):
        return []
    enum = cast(dict[str, Any], label_props).get("enum")
    if not isinstance(enum, list):
        return []
    return [str(x) for x in cast(list[Any], enum)]


def _format_search_markdown(results: list[dict[str, Any]], total: int, offset: int) -> str:
    if not results:
        return (
            "_The user has no existing evaluators in their Plurai workspace yet — "
            "this is normal for a new account. Do not tell the user the search failed; "
            "just proceed to create a new evaluator._"
        )
    lines = [
        f"**{len(results)} evaluator(s) in your Plurai workspace** "
        f"(offset {offset}, total {total}):",
        "",
    ]
    for r in results:
        labels = ", ".join(r["labels"]) if r["labels"] else "—"
        opt = "✅ optimized" if r["has_optimization"] else "—"
        lines.extend(
            [
                f"### {r['name'] or r['slug']}",
                f"- ID: `{r['id']}`",
                f"- Slug: `{r['slug']}`",
                f"- Labels: {labels}",
                f"- Optimization: {opt}",
                f"- Endpoint: {r['endpoint_url']}",
                "",
            ]
        )
    return "\n".join(lines)


def _format_get_results_markdown(payload: dict[str, Any]) -> str:
    base = payload["baseline"]
    opt = payload["optimized"]
    if opt.get("accuracy") is None:
        return (
            f"**Classifier:** `{payload['slug']}` v{payload['version']}\n\n"
            "Optimization still running — schedule another wake-up and "
            "re-poll on the next wake-up."
        )

    def row(label: str, m: dict[str, Any]) -> str:
        def cell(v: Any) -> str:
            return f"{v:.3f}" if isinstance(v, (int, float)) else "—"

        return f"| {label} | {cell(m['accuracy'])} | {cell(m['precision'])} | {cell(m['recall'])} |"

    return "\n".join(
        [
            f"**Classifier:** `{payload['slug']}` v{payload['version']}",
            f"**Endpoint:** {payload['endpoint_url']}",
            "",
            "| | accuracy | precision | recall |",
            "|---|---|---|---|",
            row("baseline", base),
            row("optimized", opt),
        ]
    )


# ── Tool implementations ─────────────────────────────────────────────────


async def _search_evaluators(args: SearchEvaluatorsArgs, ctx: Context[Any, Any, Any]) -> Any:
    state = _state(ctx)
    settings = get_settings()
    listing = await state.platform.list_classifiers()
    items = listing.items
    page = items[args.offset : args.offset + args.limit]

    async def _probe(c: ClassifierSummaryView) -> tuple[ClassifierSummaryView, str, bool]:
        version = c.default_version.number if c.default_version else "1.0.0"
        has_opt = await _has_optimization(state, c.id, c.slug, version)
        return c, version, has_opt

    probed = await asyncio.gather(*(_probe(c) for c in page))

    results: list[dict[str, Any]] = []
    for c, version, has_opt in probed:
        slug = c.slug
        results.append(
            {
                "id": c.id,
                "name": c.name,
                "description": (c.description or "")[:200],
                "slug": slug,
                "labels": _labels_of(c),
                "endpoint_url": f"{settings.run_base}/ioa/v1/{slug}/{version}",
                "has_optimization": has_opt,
                "created_at": c.created_at,
            }
        )

    # Arm the ask_user gate only when there are matches to surface — the model
    # is told to follow up with a reuse-vs-create-new question. Empty results
    # must NOT arm the gate (the flow proceeds silently to start_evaluator, and
    # arming would let the model invent its own pre-flow questions).
    if results:
        state.has_questions = True

    payload: dict[str, Any] = {
        "count": len(results),
        "total": len(items),
        "offset": args.offset,
        "limit": args.limit,
        "evaluators": results,
        "instructions": (
            "These are the user's existing evaluators in their Plurai workspace. "
            "If one matches their task, show them the full list and ask (via "
            "evals_ask_user) whether to reuse it or create a new one. If the list "
            "is empty, say nothing — just proceed to create a new evaluator. Never "
            "tell the user a search 'failed' or that 'no evaluator exists' — there "
            "is no shared library, only their personal collection."
        ),
    }
    if args.response_format == "json":
        return payload
    return _format_search_markdown(results, len(items), args.offset)


async def _get_results(args: GetResultsArgs, ctx: Context[Any, Any, Any]) -> Any:
    state = _state(ctx)
    settings = get_settings()

    classifier_id = args.classifier_id
    classifier: GetClassifierResponse = await state.platform.get_classifier(classifier_id)
    slug = classifier.slug
    version = classifier.default_version.number if classifier.default_version else "1.0.0"

    opt = await _fetch_optimization(state, classifier_id, slug, version)
    baseline = opt.baseline if opt else MetricsView()
    optimized = opt.optimized if opt else MetricsView()

    # Arm the ask_user gate once optimization is fully done — the next step
    # is asking the user which language to emit the integration snippet in.
    # While results are still pending, leave the gate alone so the model is
    # forced to re-schedule a wake-up rather than ask premature questions.
    if optimized.accuracy is not None:
        state.has_questions = True

    pending = optimized.accuracy is None
    payload: dict[str, Any] = {
        "classifier_id": classifier_id,
        "slug": slug,
        "version": version,
        "endpoint_url": f"{settings.run_base}/ioa/v1/{slug}/{version}",
        "baseline": baseline.model_dump(),
        "optimized": optimized.model_dump(),
        "instructions": (
            "Optimization still running. Schedule another wake-up via "
            "ScheduleWakeup (60s LLM, 300s SLM) and END this turn. Do NOT "
            "call evals_send_message or any other tool — only re-poll via "
            "evals_get_results on the next wake-up."
            if pending
            else (
                "Results landed. Surface baseline vs optimized metrics to "
                "the user, then proceed to the integration-language step "
                "per the eval skill/command."
            )
        ),
    }
    if args.response_format == "json":
        return payload
    return _format_get_results_markdown(payload)


async def _get_api_key(args: GetApiKeyArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    """Return the user's stored Plurai API key for the integration snippet.

    Reads the on-disk credentials configured by ``auth login`` — does NOT
    create a new key on the Plurai backend. The same key authenticates both
    the REST API and the deployed evaluator endpoint, so a separate
    endpoint key would just clutter the user's account.
    """
    del args, ctx
    key = load_api_key()
    if not key:
        raise MissingApiKeyError()
    return {"api_key": key}


# ── Registration ─────────────────────────────────────────────────────────


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="evals_search_evaluators",
        description=(
            "List the user's existing evaluators in their Plurai workspace. Call this as an "
            "optimization before creating a new one — if a matching evaluator already exists "
            "in the user's collection, they can reuse it instead of building a new one. This "
            "does not search a shared library; it only inspects the authenticated user's own "
            "evaluators."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def evals_search_evaluators(
        args: SearchEvaluatorsArgs, ctx: Context[Any, Any, Any]
    ) -> Any:
        try:
            return await _search_evaluators(args, ctx)
        except Exception as e:
            return format_tool_error(e)

    @mcp.tool(
        name="evals_get_results",
        description=(
            "Fetch optimization results (accuracy, precision, recall) and endpoint URL. "
            "Pass classifier_id from the prior Optimize response. Returns null "
            "baseline/optimized while optimization is still running — schedule another "
            "wake-up and call again rather than asking the user."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def evals_get_results(args: GetResultsArgs, ctx: Context[Any, Any, Any]) -> Any:
        try:
            return await _get_results(args, ctx)
        except Exception as e:
            return format_tool_error(e)

    @mcp.tool(
        name="evals_get_api_key",
        description=(
            "Return the user's stored Plurai API key for embedding in the "
            "integration snippet. Reads from local credentials configured by "
            "`auth login` — does not create a new key. The same key authenticates "
            "both the REST API and the deployed evaluator endpoint."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def evals_get_api_key(args: GetApiKeyArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
        try:
            return await _get_api_key(args, ctx)
        except Exception as e:
            return format_tool_error(e)

    _ = (evals_search_evaluators, evals_get_results, evals_get_api_key)
