"""Data-upload tool: pluto_upload_data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

import httpx
from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from ..config import PLUTO_API
from ..errors import safe_error_body
from ..state import ServerState

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

_StrictModel = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UploadRecord(BaseModel):
    model_config = _StrictModel
    sample: Annotated[str, Field(description="The text being labeled.")]
    label: Annotated[str, Field(description="Ground-truth label for the sample.")]
    reasoning: Annotated[str, Field(default="", description="Optional rationale for the label.")]


class UploadDataArgs(BaseModel):
    model_config = _StrictModel
    example_set_id: Annotated[
        str, Field(description="Example set ID returned by pluto_start_judge.")
    ]
    records: Annotated[
        list[UploadRecord],
        Field(description="Labeled examples read from the user's file."),
    ]
    file_name: Annotated[str, Field(default="examples.csv", description="Original file name.")]
    source: Annotated[str, Field(default="", description="Free-form provenance string.")]


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="pluto_upload_data",
        description=(
            "Upload labeled examples from a user-provided file. Only use when the user "
            "explicitly provides a data file path."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def pluto_upload_data(
        args: UploadDataArgs, ctx: Context[Any, Any, Any]
    ) -> dict[str, Any]:
        state = cast(ServerState, ctx.request_context.lifespan_context)
        records = [r.model_dump() for r in args.records]
        try:
            await state.pluto.request(
                "POST",
                f"{PLUTO_API}/example-sets/{args.example_set_id}/files",
                json_body={"fileName": args.file_name, "records": records},
                timeout=60.0,
            )
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code}: {safe_error_body(e)}"}
        return {"status": "uploaded", "count": len(records), "source": args.source}

    _ = pluto_upload_data
