"""Shared HTTP client infrastructure: retry and dynamic headers.

Auth headers are fetched per-request via ``headers_provider``; the
provider is expected to track its own source (e.g. mtime polling on a
credentials file) so that a fresh `auth login` is picked up on the very
next request without a restart. 401 propagates to the caller — the
provider, not this client, owns refresh policy.

Subclasses set ``_client_label`` and add domain-specific methods on top
of :meth:`_request_authed` (JSON requests).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Self

import httpx
import structlog
from pydantic import BaseModel, Field, model_validator
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger: Any = structlog.get_logger(__name__)

HeadersProvider = Callable[[], Mapping[str, str]]


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS_CODES
    return isinstance(exc, httpx.TransportError)


def _make_log_retry(client_label: str) -> Callable[[RetryCallState], None]:
    def _log_retry(state: RetryCallState) -> None:
        exc = state.outcome and state.outcome.exception()
        logger.warning(
            f"Retry attempt for {client_label} request",
            attempt=state.attempt_number,
            error=str(exc),
        )

    return _log_retry


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class BaseHttpClientConfig(BaseModel):
    """Shared configuration fields for HTTP API clients."""

    api_url: str = Field(..., min_length=1)
    timeout: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    backoff_base: float = Field(default=1.0, gt=0)
    backoff_max: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def validate_backoff_range(self) -> BaseHttpClientConfig:
        if self.backoff_base > self.backoff_max:
            raise ValueError(
                f"backoff_base ({self.backoff_base}) must be <= backoff_max ({self.backoff_max})"
            )
        return self


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class BaseHttpClient:
    """Async HTTP client with retry and dynamic headers.

    Auth headers are fetched per-request via ``headers_provider``; the
    provider is responsible for refresh policy (typically mtime polling
    on a credentials file). 401 propagates to the caller. Tenacity
    handles transient failures (5xx, 429, 408, transport errors).
    """

    _client_label: str = "HTTP"

    def __init__(
        self,
        config: BaseHttpClientConfig,
        *,
        headers_provider: HeadersProvider,
    ) -> None:
        self._config = config
        self._headers_provider = headers_provider
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            base_url=self._config.api_url,
            timeout=self._config.timeout,
        )
        return self

    async def __aexit__(self, *_args: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(f"{type(self).__name__} must be used as an async context manager")
        return self._client

    # -- Core JSON request path ------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        expected_error_codes: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {"params": params, "json": json_body, "headers": headers}
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = await self._http.request(method, path, **kwargs)
        if resp.status_code in expected_error_codes:
            return resp
        if resp.is_error:
            log = logger.warning if resp.status_code in _RETRYABLE_STATUS_CODES else logger.error
            log(
                f"{self._client_label} error",
                method=method,
                path=path,
                status_code=resp.status_code,
                response=resp.text,
            )
        resp.raise_for_status()
        return resp

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        expected_error_codes: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._config.max_retries + 1),
            wait=wait_exponential(
                multiplier=self._config.backoff_base,
                min=self._config.backoff_base,
                max=self._config.backoff_max,
            ),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
            before_sleep=_make_log_retry(self._client_label),
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await self._request(
                        method,
                        path,
                        params=params,
                        json_body=json_body,
                        headers=headers,
                        timeout=timeout,
                        expected_error_codes=expected_error_codes,
                    )
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if _is_retryable(exc):
                logger.error(
                    f"{self._client_label} retries exhausted",
                    method=method,
                    path=path,
                )
            raise
        # Unreachable: AsyncRetrying either returns or raises.
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _request_authed(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
        expected_error_codes: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        """JSON request with dynamic auth headers.

        Status codes in ``expected_error_codes`` are returned to the caller
        without logging or ``raise_for_status``, so callers handling a known
        non-2xx response (e.g. 404 = "not found yet") don't spam the log.
        Must not overlap with retryable codes — "expected" is a terminal
        outcome by definition, so suppressing one would also suppress the
        retry loop.
        """
        overlap = expected_error_codes & _RETRYABLE_STATUS_CODES
        if overlap:
            raise ValueError(
                f"expected_error_codes overlaps retryable status codes {sorted(overlap)}; "
                "marking a code as expected would silently disable retries for it"
            )
        return await self._request_with_retry(
            method,
            path,
            params=params,
            json_body=json_body,
            headers=self._headers_provider(),
            timeout=timeout,
            expected_error_codes=expected_error_codes,
        )
