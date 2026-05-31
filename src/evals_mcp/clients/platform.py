"""Typed Plurai REST API client.

Inherits :class:`BaseHttpClient` for retry, dynamic auth, and SSE
plumbing; exposes only the endpoints the evals MCP server actually calls.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Literal, cast

from .base import BaseHttpClient
from .models import (
    CreateApiKeyRequest,
    CreateApiKeyResponse,
    CreateExampleFileRequest,
    CreateExampleFileResponse,
    GetClassifierResponse,
    ListClassifiersResponse,
    OptimizationView,
    PlanResponse,
    ThreadView,
)


class PlatformClient(BaseHttpClient):
    """Async client for the Plurai REST API."""

    _client_label = "Plurai API"

    # -- Threads ---------------------------------------------------------------

    async def create_thread(
        self,
        workflow: Literal["with-data", "without-data"] = "with-data",
    ) -> ThreadView:
        resp = await self._request_authed("POST", "/threads", json_body={"workflow": workflow})
        payload = cast(dict[str, Any], resp.json())
        # Tolerate the legacy `{"items": [thread]}` envelope.
        if "id" not in payload and isinstance(payload.get("items"), list):
            items = cast(list[dict[str, Any]], payload["items"])
            if items:
                payload = items[0]
        return ThreadView.model_validate(payload)

    # -- Classifiers -----------------------------------------------------------

    async def list_classifiers(self) -> ListClassifiersResponse:
        resp = await self._request_authed("GET", "/classifiers")
        return ListClassifiersResponse.model_validate(resp.json())

    async def get_classifier(self, classifier_id: str) -> GetClassifierResponse:
        resp = await self._request_authed("GET", f"/classifiers/{classifier_id}")
        return GetClassifierResponse.model_validate(resp.json())

    async def get_optimization(self, identifier: str, version: str) -> OptimizationView | None:
        """Fetch optimization results. Returns ``None`` on 404 (no run yet)."""
        resp = await self._request_authed(
            "GET",
            f"/classifiers/{identifier}/versions/{version}/optimization",
            expected_error_codes=frozenset({HTTPStatus.NOT_FOUND}),
        )
        if resp.status_code == HTTPStatus.NOT_FOUND:
            return None
        return OptimizationView.model_validate(resp.json())

    # -- Plan ------------------------------------------------------------------

    async def get_plan(self) -> PlanResponse:
        resp = await self._request_authed("GET", "/plan")
        return PlanResponse.model_validate(resp.json())

    # -- API keys --------------------------------------------------------------

    async def create_api_key(self, name: str) -> CreateApiKeyResponse:
        request = CreateApiKeyRequest(name=name)
        resp = await self._request_authed(
            "POST",
            "/api-keys",
            json_body=request.model_dump(by_alias=True),
        )
        return CreateApiKeyResponse.model_validate(resp.json())

    # -- Example-set uploads ---------------------------------------------------

    async def upload_example_file(
        self,
        example_set_id: str,
        request: CreateExampleFileRequest,
        *,
        timeout: float | None = None,
    ) -> CreateExampleFileResponse:
        resp = await self._request_authed(
            "POST",
            f"/example-sets/{example_set_id}/files",
            json_body=request.model_dump(by_alias=True),
            timeout=timeout,
        )
        return CreateExampleFileResponse.model_validate(resp.json())
