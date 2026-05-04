"""Tests for the lifespan auth integration in state.py.

Covers the cache lifecycle that links auth.py to the HTTP clients:
- startup with no key must not crash (so /login can recover)
- ``headers_provider`` lazily resolves on first call
- ``auth_refresh`` re-reads after the user logs in
- a failed refresh clears the cache instead of serving stale headers
- background tasks get cancelled (not awaited) on shutdown
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from evals_mcp import auth
from evals_mcp.errors import MissingApiKeyError
from evals_mcp.state import lifespan


@pytest.fixture
def creds_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / ".config" / "evals" / "credentials.json"


async def test_lifespan_boots_without_a_key(
    creds_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Server must start even when no key is configured — the whole point of
    the lazy cache is that ``/login`` can recover the session."""
    _ = creds_path
    async with lifespan(None) as state:  # type: ignore[arg-type]
        assert state.platform is not None
        assert state.agent is not None
    err = capsys.readouterr().err
    assert "no API key at startup" in err


async def test_lifespan_caches_key_at_startup(creds_path: Path) -> None:
    _ = creds_path
    auth.save_api_key("ak_initial")
    async with lifespan(None) as state:  # type: ignore[arg-type]
        # Both clients must share the same headers_provider closure so
        # auth_refresh updates them in lockstep.
        h1 = await state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]
        h2 = await state.agent._headers_provider()  # pyright: ignore[reportPrivateUsage]
        assert h1 == h2 == {"Authorization": "Bearer ak_initial"}


async def test_headers_provider_resolves_lazily_after_late_login(creds_path: Path) -> None:
    """User boots without a key, then runs /login mid-session."""
    _ = creds_path
    async with lifespan(None) as state:  # type: ignore[arg-type]
        # Boot was unauthenticated; provider should still raise.
        with pytest.raises(MissingApiKeyError):
            await state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]

        # User now logs in.
        auth.save_api_key("ak_late")
        headers = await state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]
        assert headers == {"Authorization": "Bearer ak_late"}


async def test_auth_refresh_picks_up_new_key(creds_path: Path) -> None:
    _ = creds_path
    auth.save_api_key("ak_old")
    async with lifespan(None) as state:  # type: ignore[arg-type]
        first = await state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]
        assert first == {"Authorization": "Bearer ak_old"}

        auth.save_api_key("ak_new")
        # Without auth_refresh, the cache would still hand back ak_old.
        await state.platform._auth_refresh()  # type: ignore[misc]  # pyright: ignore[reportPrivateUsage]
        refreshed = await state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]
        assert refreshed == {"Authorization": "Bearer ak_new"}


async def test_auth_refresh_failure_clears_cache(creds_path: Path) -> None:
    """If refresh fails, subsequent requests must not serve the previously
    cached (now-revoked) headers — they should fail loudly so the model
    prompts re-login instead of silently using a dead key."""
    _ = creds_path
    auth.save_api_key("ak_old")
    async with lifespan(None) as state:  # type: ignore[arg-type]
        await state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]  # populate cache

        auth.delete_api_key()
        with pytest.raises(MissingApiKeyError):
            await state.platform._auth_refresh()  # type: ignore[misc]  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(MissingApiKeyError):
            await state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]


async def test_lifespan_cancels_background_tasks_on_shutdown(creds_path: Path) -> None:
    """A background task that would otherwise run for minutes (e.g. an SLM
    optimize SSE stream) must be cancelled on lifespan exit, not awaited
    to natural completion. Otherwise shutdown blocks and the host
    force-kills us."""
    _ = creds_path

    captured: dict[str, asyncio.Task[None]] = {}

    async def long_runner() -> None:
        await asyncio.sleep(60)

    start = time.monotonic()
    async with lifespan(None) as state:  # type: ignore[arg-type]
        task = asyncio.create_task(long_runner())
        state.background_tasks.add(task)
        captured["task"] = task
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"shutdown took {elapsed:.2f}s — task wasn't cancelled"
    assert captured["task"].cancelled()
