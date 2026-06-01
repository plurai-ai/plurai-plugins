"""Data-upload tool: upload_data."""

from __future__ import annotations

from typing import Annotated, Any, cast

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from ..clients import CreateExampleFileRequest, ExampleRecordInput
from ..errors import format_tool_error
from ..state import ServerState

_StrictModel = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UploadRecord(BaseModel):
    model_config = _StrictModel
    sample: Annotated[str, Field(description="The text being labeled.")]
    label: Annotated[str, Field(description="Ground-truth label for the sample.")]
    reasoning: Annotated[str, Field(default="", description="Optional rationale for the label.")]


class UploadDataArgs(BaseModel):
    model_config = _StrictModel
    example_set_id: Annotated[
        str,
        Field(min_length=1, description="Example set ID returned by start_evaluator."),
    ]
    records: Annotated[
        list[UploadRecord],
        Field(min_length=1, description="Labeled examples read from the user's file."),
    ]
    file_name: Annotated[
        str, Field(default="examples.csv", min_length=1, description="Original file name.")
    ]
    source: Annotated[str, Field(default="", description="Free-form provenance string.")]


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="upload_data",
        description=(
            "Upload labeled examples from a user-provided file. Requires `example_set_id` "
            "returned by start_evaluator. Only use when the user explicitly provides a "
            "data file path — do NOT synthesize records."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def upload_data(args: UploadDataArgs, ctx: Context[Any, Any, Any]) -> dict[str, Any]:
        state = cast(ServerState, ctx.request_context.lifespan_context)
        request = CreateExampleFileRequest(
            file_name=args.file_name,
            records=[
                ExampleRecordInput(sample=r.sample, label=r.label, reasoning=r.reasoning)
                for r in args.records
            ],
        )
        try:
            await state.platform.upload_example_file(args.example_set_id, request, timeout=60.0)
        except Exception as e:
            return format_tool_error(e)
        return {"status": "uploaded", "count": len(args.records), "source": args.source}

    _ = upload_data
