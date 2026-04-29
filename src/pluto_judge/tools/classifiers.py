"""Classifier tools: search_evaluators, get_results, create_api_key."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import httpx
from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from ..config import PLUTO_API, RUN_BASE
from ..errors import safe_error_body
from ..state import ServerState

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

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
        str, Field(description="Classifier ID returned by pluto_send_message.")
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


def _metrics(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "accuracy": m.get("accuracy"),
        "precision": m.get("precision"),
        "recall": m.get("recall"),
    }


async def _has_optimization(
    state: ServerState, classifier_uuid: str, slug: str, version: str
) -> bool:
    for identifier in (classifier_uuid, slug):
        try:
            await state.pluto.request(
                "GET",
                f"{PLUTO_API}/classifiers/{identifier}/versions/{version}/optimization",
            )
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                continue
            raise
    return False


async def _fetch_optimization(
    state: ServerState, classifier_uuid: str, slug: str, version: str
) -> dict[str, Any] | None:
    for identifier in (classifier_uuid, slug):
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
    listing = await state.pluto.request("GET", f"{PLUTO_API}/classifiers")
    items: list[dict[str, Any]] = listing.get("items", [])
    page = items[args.offset : args.offset + args.limit]

    results: list[dict[str, Any]] = []
    for c in page:
        slug = c.get("slug", "")
        version = c.get("defaultVersion", {}).get("number", "1.0.0")
        has_opt = await _has_optimization(state, c["id"], slug, version)
        labels = list(
            c.get("outputSchema", {}).get("properties", {}).get("label", {}).get("enum", [])
        )
        results.append(
            {
                "id": c["id"],
                "name": c.get("name", ""),
                "description": (c.get("description") or "")[:200],
                "slug": slug,
                "labels": labels,
                "endpoint_url": f"{RUN_BASE}/ioa/v1/{slug}/{version}",
                "has_optimization": has_opt,
                "created_at": c.get("createdAt", ""),
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
    classifier = await state.pluto.request("GET", f"{PLUTO_API}/classifiers/{args.classifier_id}")
    slug: str = classifier["slug"]
    version: str = classifier.get("defaultVersion", {}).get("number", "1.0.0")

    opt = await _fetch_optimization(state, args.classifier_id, slug, version)
    baseline: dict[str, Any] = (opt or {}).get("baseline", {})
    optimized: dict[str, Any] = (opt or {}).get("optimized", {})

    payload: dict[str, Any] = {
        "classifier_id": args.classifier_id,
        "slug": slug,
        "version": version,
        "endpoint_url": f"{RUN_BASE}/ioa/v1/{slug}/{version}",
        "baseline": _metrics(baseline),
        "optimized": _metrics(optimized),
    }
    if args.response_format == "json":
        return payload
    return _format_get_results_markdown(payload)


async def _create_api_key(args: CreateApiKeyArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    state = _state(ctx)
    result = await state.pluto.request(
        "POST", f"{PLUTO_API}/api-keys", json_body={"name": args.name}
    )
    return {"api_key": result["secret"], "key_id": result["id"]}


# ── Registration ─────────────────────────────────────────────────────────


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="pluto_search_evaluators",
        description=(
            "Search existing evaluators on the Pluto platform. Call this first to check if a "
            "relevant evaluator already exists before creating a new one."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def pluto_search_evaluators(
        args: SearchEvaluatorsArgs, ctx: Context[Any, Any, Any]
    ) -> Any:
        try:
            return await _search_evaluators(args, ctx)
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code}: {safe_error_body(e)}"}

    @mcp.tool(
        name="pluto_get_results",
        description="Get optimization results (accuracy, precision, recall) and endpoint URL.",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def pluto_get_results(args: GetResultsArgs, ctx: Context[Any, Any, Any]) -> Any:
        try:
            return await _get_results(args, ctx)
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code}: {safe_error_body(e)}"}

    @mcp.tool(
        name="pluto_create_api_key",
        description="Generate an API key for the evaluator endpoint.",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def pluto_create_api_key(
        args: CreateApiKeyArgs, ctx: Context[Any, Any, Any]
    ) -> dict[str, Any]:
        try:
            return await _create_api_key(args, ctx)
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code}: {safe_error_body(e)}"}

    _ = (pluto_search_evaluators, pluto_get_results, pluto_create_api_key)
