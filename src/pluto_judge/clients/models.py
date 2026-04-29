"""Pydantic request/response models for the Pluto and Agent clients."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# ---------------------------------------------------------------------------
# Base model config
# ---------------------------------------------------------------------------

# Strict for inputs we send (we know the schema). For server responses we
# tolerate unknown fields — APIs add new properties without breaking us.

_StrictModel = ConfigDict(
    extra="forbid",
    str_strip_whitespace=True,
    populate_by_name=True,
    alias_generator=to_camel,
)
_LooseModel = ConfigDict(
    extra="ignore",
    str_strip_whitespace=True,
    populate_by_name=True,
    alias_generator=to_camel,
)


# ---------------------------------------------------------------------------
# REST: Threads
# ---------------------------------------------------------------------------


class ThreadView(BaseModel):
    model_config = _LooseModel

    id: str
    example_set_id: str = ""
    dataset_id: str | None = None
    name: str | None = None


# ---------------------------------------------------------------------------
# REST: Classifiers
# ---------------------------------------------------------------------------


class ClassifierDefaultVersionView(BaseModel):
    model_config = _LooseModel

    id: str | None = None
    number: str = "1.0.0"


class ClassifierSummaryView(BaseModel):
    model_config = _LooseModel

    id: str
    slug: str = ""
    name: str = ""
    description: str | None = None
    default_version: ClassifierDefaultVersionView | None = None
    output_schema: dict[str, Any] = Field(default_factory=lambda: {})
    created_at: str = ""


class ListClassifiersResponse(BaseModel):
    model_config = _LooseModel

    items: list[ClassifierSummaryView] = Field(default_factory=lambda: [])


class GetClassifierResponse(BaseModel):
    model_config = _LooseModel

    id: str | None = None
    slug: str
    name: str = ""
    description: str | None = None
    default_version: ClassifierDefaultVersionView | None = None
    output_schema: dict[str, Any] = Field(default_factory=lambda: {})


# ---------------------------------------------------------------------------
# REST: Optimization results
# ---------------------------------------------------------------------------


class MetricsView(BaseModel):
    model_config = _LooseModel

    accuracy: float | None = None
    precision: float | None = None
    recall: float | None = None


class OptimizationView(BaseModel):
    model_config = _LooseModel

    baseline: MetricsView = Field(default_factory=MetricsView)
    optimized: MetricsView = Field(default_factory=MetricsView)


# ---------------------------------------------------------------------------
# REST: API keys
# ---------------------------------------------------------------------------


class CreateApiKeyRequest(BaseModel):
    model_config = _StrictModel

    name: str


class CreateApiKeyResponse(BaseModel):
    model_config = _LooseModel

    id: str
    secret: str


# ---------------------------------------------------------------------------
# REST: Example-set file uploads
# ---------------------------------------------------------------------------


class ExampleRecordInput(BaseModel):
    model_config = _StrictModel

    sample: str
    label: str
    reasoning: str = ""


class CreateExampleFileRequest(BaseModel):
    model_config = _StrictModel

    file_name: str
    records: list[ExampleRecordInput]


class CreateExampleFileResponse(BaseModel):
    model_config = _LooseModel

    id: str | None = None
    file_name: str | None = None
    example_set_id: str | None = None


# ---------------------------------------------------------------------------
# Agent: CopilotKit envelope + events
# ---------------------------------------------------------------------------


class AgentMessage(BaseModel):
    model_config = _StrictModel

    id: str
    role: Literal["user", "assistant"]
    content: str


class AgentRunBody(BaseModel):
    model_config = _StrictModel

    thread_id: str
    run_id: str
    state: dict[str, Any] = Field(default_factory=lambda: {})
    messages: list[AgentMessage]
    tools: list[Any] = Field(default_factory=lambda: [])
    context: list[Any] = Field(default_factory=lambda: [])
    forwarded_props: dict[str, Any] = Field(default_factory=lambda: {})


class AgentEnvelope(BaseModel):
    model_config = _StrictModel

    method: Literal["agent/run"]
    params: dict[str, Any]
    body: AgentRunBody


class AgentEvent(BaseModel):
    """A single SSE event from the CopilotKit agent.

    Loose by design: the agent emits multiple event types (MESSAGES_SNAPSHOT,
    STATE_SNAPSHOT, TOOL_CALL, …) and we don't model every variant. Tools
    introspect ``type`` and read whatever sibling fields they need via
    ``model_extra`` / ``model_dump``.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str = ""
