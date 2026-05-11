"""Typed HTTP clients for the Plurai REST API and the agent endpoint."""

from .agent import AgentClient, ThreadStateView
from .base import BaseHttpClient, BaseHttpClientConfig, HeadersProvider
from .models import (
    ClassifierDefaultVersionView,
    ClassifierSummaryView,
    CreateApiKeyResponse,
    CreateExampleFileRequest,
    CreateExampleFileResponse,
    ExampleRecordInput,
    GetClassifierResponse,
    ListClassifiersResponse,
    MetricsView,
    OptimizationView,
    ThreadView,
)
from .platform import PlatformClient

__all__ = [
    "AgentClient",
    "BaseHttpClient",
    "BaseHttpClientConfig",
    "ClassifierDefaultVersionView",
    "ClassifierSummaryView",
    "CreateApiKeyResponse",
    "CreateExampleFileRequest",
    "CreateExampleFileResponse",
    "ExampleRecordInput",
    "GetClassifierResponse",
    "HeadersProvider",
    "ListClassifiersResponse",
    "MetricsView",
    "OptimizationView",
    "PlatformClient",
    "ThreadStateView",
    "ThreadView",
]
