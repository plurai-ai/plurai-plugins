"""Async HTTP client wrapper with 401-retry and SSE support.

Wraps a single shared `httpx.AsyncClient` (created in lifespan) with:

- JSON request/response (`request`)
- Server-Sent Events POST → list of decoded `data:` events (`stream_sse`)
- A single retry on HTTP 401 that calls the backend's `force_login()`
  (which may pop a browser flow) before retrying with fresh headers.

Auth is supplied as a sync `headers_fn` and a sync `force_login_fn`; both
are invoked through `asyncio.to_thread` so the event loop never blocks on
the (rare) interactive login path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx

HeadersFn = Callable[[], dict[str, str]]
ForceLoginFn = Callable[[], Any]


class PlutoClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        headers_fn: HeadersFn,
        force_login_fn: ForceLoginFn,
    ) -> None:
        self._client = client
        self._headers_fn = headers_fn
        self._force_login = force_login_fn

    async def _headers(self) -> dict[str, str]:
        return await asyncio.to_thread(self._headers_fn)

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        timeout: float = 30.0,
    ) -> Any:
        headers = await self._headers()
        try:
            return await self._send(method, url, headers, json_body, timeout)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 401:
                raise
            await asyncio.to_thread(self._force_login)
            headers = await self._headers()
            return await self._send(method, url, headers, json_body, timeout)

    async def _send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: Any,
        timeout: float,
    ) -> Any:
        r = await self._client.request(
            method, url, json=json_body, headers=headers, timeout=timeout
        )
        r.raise_for_status()
        return r.json()

    async def stream_sse(
        self,
        url: str,
        json_body: dict[str, Any],
        *,
        timeout: float = 300.0,
    ) -> list[dict[str, Any]]:
        headers = await self._headers()
        try:
            return await self._sse_send(url, headers, json_body, timeout)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 401:
                raise
            await asyncio.to_thread(self._force_login)
            headers = await self._headers()
            return await self._sse_send(url, headers, json_body, timeout)

    async def _sse_send(
        self,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any],
        timeout: float,
    ) -> list[dict[str, Any]]:
        send_headers = {**headers, "Accept": "text/event-stream"}
        events: list[dict[str, Any]] = []
        async with self._client.stream(
            "POST", url, json=json_body, headers=send_headers, timeout=timeout
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                stripped = line.strip()
                if not stripped.startswith("data: "):
                    continue
                try:
                    events.append(json.loads(stripped[6:]))
                except json.JSONDecodeError:
                    continue
        return events
