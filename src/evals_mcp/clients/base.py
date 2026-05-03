"""Shared HTTP client infrastructure: retry, dynamic auth, and SSE.

Auth is supplied per-request via ``headers_provider``; an optional
``auth_refresh`` hook is invoked once on 401 so the client can
transparently re-authenticate (Clerk JWT, Chrome cookies, broker flow).

Subclasses set ``_client_label`` and add domain-specific methods on top
of :meth:`_request_authed` (JSON requests) and :meth:`_stream_sse_authed`
(server-sent events).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Self

import httpx
import structlog
from httpx_sse import aconnect_sse
from pydantic import BaseModel, Field, model_validator
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger: Any = structlog.get_logger(__name__)

HeadersProvider = Callable[[], Awaitable[Mapping[str, str]]]
AuthRefresh = Callable[[], Awaitable[None]]


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
    """Async HTTP client with retry, dynamic auth, and SSE support.

    Auth is supplied per-request via ``headers_provider``; on a 401, the
    optional ``auth_refresh`` hook is invoked once and the request retried
    with refreshed headers. Tenacity handles transient failures (5xx, 429,
    408, transport errors).
    """

    _client_label: str = "HTTP"

    def __init__(
        self,
        config: BaseHttpClientConfig,
        *,
        headers_provider: HeadersProvider,
        auth_refresh: AuthRefresh | None = None,
    ) -> None:
        self._config = config
        self._headers_provider = headers_provider
        self._auth_refresh = auth_refresh
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
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {"params": params, "json": json_body, "headers": headers}
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = await self._http.request(method, path, **kwargs)
        if resp.is_error:
            # 401 is downgraded to debug because `_request_authed` will
            # invoke `auth_refresh` and retry — escalating only matters
            # if the *retry* fails, which is logged separately.
            if resp.status_code == 401 and self._auth_refresh is not None:
                log = logger.debug
            elif resp.status_code in _RETRYABLE_STATUS_CODES:
                log = logger.warning
            else:
                log = logger.error
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
    ) -> httpx.Response:
        """JSON request with dynamic auth and 401-triggered refresh."""
        headers = dict(await self._headers_provider())
        try:
            return await self._request_with_retry(
                method,
                path,
                params=params,
                json_body=json_body,
                headers=headers,
                timeout=timeout,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401 or self._auth_refresh is None:
                raise
            try:
                await self._auth_refresh()
            except Exception as refresh_exc:
                raise RuntimeError(
                    f"Auth refresh failed during {method} {path}: {refresh_exc}"
                ) from refresh_exc
            headers = dict(await self._headers_provider())
            return await self._request_with_retry(
                method,
                path,
                params=params,
                json_body=json_body,
                headers=headers,
                timeout=timeout,
            )

    # -- SSE streaming path ---------------------------------------------------

    async def _stream_sse_authed(
        self,
        path: str,
        json_body: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """POST + parse a Server-Sent Events response into decoded events.

        Tenacity retry is intentionally skipped — long-lived streams should
        not be retried mid-flight. The 401-refresh-once hook still applies.
        ``timeout=None`` falls back to the configured client timeout.
        """
        effective_timeout = timeout if timeout is not None else self._config.timeout
        headers = dict(await self._headers_provider())
        try:
            return await self._sse_send(path, headers, json_body, effective_timeout)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401 or self._auth_refresh is None:
                raise
            try:
                await self._auth_refresh()
            except Exception as refresh_exc:
                raise RuntimeError(
                    f"Auth refresh failed during SSE POST {path}: {refresh_exc}"
                ) from refresh_exc
            headers = dict(await self._headers_provider())
            return await self._sse_send(path, headers, json_body, effective_timeout)

    async def _sse_send(
        self,
        path: str,
        headers: dict[str, str],
        json_body: Mapping[str, Any],
        timeout: float,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async with aconnect_sse(
            self._http,
            "POST",
            path,
            headers=headers,
            json=json_body,
            timeout=timeout,
        ) as event_source:
            event_source.response.raise_for_status()
            async for sse in event_source.aiter_sse():
                if not sse.data:
                    continue
                try:
                    events.append(json.loads(sse.data))
                except json.JSONDecodeError as exc:
                    # Don't kill the stream on a single corrupt event;
                    # log so silent truncation is debuggable.
                    logger.warning(
                        f"Dropped malformed SSE event in {self._client_label}",
                        path=path,
                        error=str(exc),
                        sample=sse.data[:200],
                    )
                    continue
        return events
