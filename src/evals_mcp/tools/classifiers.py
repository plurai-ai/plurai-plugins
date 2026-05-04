"""Classifier tools: search_evaluators, get_results, create_api_key."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal, cast

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from ..clients import (
    ClassifierSummaryView,
    GetClassifierResponse,
    MetricsView,
    OptimizationView,
)
from ..config import get_settings
from ..errors import format_tool_error
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
        str, Field(description="Classifier ID returned by evals_send_message.")
    ]
    response_format: Annotated[
        ResponseFormat,
        Field(
            default="markdown",
            description="'markdown' for human display, 'json' for machine-readable.",
        ),
    ]


class CreateApiKeyArgs(BaseModel):
    model_config = _StrictModel
    name: Annotated[
        str,
        Field(default="judge-endpoint", description="Display name for the API key."),
    ]


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
        return "_No evaluators found._"
    lines = [
        f"**{len(results)} evaluator(s)** (offset {offset}, total {total}):",
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
                "endpoint_url": f"{settings.run_url}/ioa/v1/{slug}/{version}",
                "has_optimization": has_opt,
                "created_at": c.created_at,
            }
        )

    payload: dict[str, Any] = {
        "count": len(results),
        "total": len(items),
        "offset": args.offset,
        "limit": args.limit,
        "evaluators": results,
        "instructions": (
            "Show the user the existing evaluators. If one matches their task, "
            "ask if they want to reuse it (use its endpoint) or create a new one."
        ),
    }
    if args.response_format == "json":
        return payload
    return _format_search_markdown(results, len(items), args.offset)


async def _get_results(args: GetResultsArgs, ctx: Context[Any, Any, Any]) -> Any:
    state = _state(ctx)
    settings = get_settings()
    classifier: GetClassifierResponse = await state.platform.get_classifier(args.classifier_id)
    slug = classifier.slug
    version = classifier.default_version.number if classifier.default_version else "1.0.0"

    opt = await _fetch_optimization(state, args.classifier_id, slug, version)
    baseline = opt.baseline if opt else MetricsView()
    optimized = opt.optimized if opt else MetricsView()

    payload: dict[str, Any] = {
        "classifier_id": args.classifier_id,
        "slug": slug,
        "version": version,
        "endpoint_url": f"{settings.run_url}/ioa/v1/{slug}/{version}",
        "baseline": baseline.model_dump(),
        "optimized": optimized.model_dump(),
    }
    if args.response_format == "json":
        return payload
    return _format_get_results_markdown(payload)


async def _create_api_key(args: CreateApiKeyArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    state = _state(ctx)
    result = await state.platform.create_api_key(args.name)
    return {"api_key": result.secret, "key_id": result.id}


# ── Registration ─────────────────────────────────────────────────────────


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="evals_search_evaluators",
        description=(
            "Search existing evaluators on the Plurai platform. Call this first to check if a "
            "relevant evaluator already exists before creating a new one."
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
        description="Get optimization results (accuracy, precision, recall) and endpoint URL.",
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
        name="evals_create_api_key",
        description="Generate an API key for the evaluator endpoint.",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def evals_create_api_key(
        args: CreateApiKeyArgs, ctx: Context[Any, Any, Any]
    ) -> dict[str, Any]:
        try:
            return await _create_api_key(args, ctx)
        except Exception as e:
            return format_tool_error(e)

    _ = (evals_search_evaluators, evals_get_results, evals_create_api_key)
