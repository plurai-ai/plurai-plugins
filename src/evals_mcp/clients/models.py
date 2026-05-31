"""Pydantic request/response models for the Plurai platform and Agent clients."""

from __future__ import annotations

from typing import Any

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
# REST: Subscription plan
# ---------------------------------------------------------------------------


class PlanEntitlements(BaseModel):
    model_config = _LooseModel

    llm_endpoints: bool = False
    slm_endpoints: bool = False
    thread_count_limit: int | None = None


class PlanResponse(BaseModel):
    model_config = _LooseModel

    id: str
    name: str = ""
    entitlements: PlanEntitlements = Field(default_factory=PlanEntitlements)
