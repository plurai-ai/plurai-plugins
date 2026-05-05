"""Tests for the lifespan auth integration in state.py.

Covers the cache lifecycle that links auth.py to the HTTP clients:
- startup with no key must not crash (so a mid-session `auth login` can
  recover)
- ``headers_provider`` lazily resolves on first call
- ``headers_provider`` re-reads after the credentials file changes
  (mtime/inode tracking — picks up a mid-session `auth login`
  transparently)
- a missing file after a successful read raises instead of serving
  stale headers
- background tasks get cancelled (not awaited) on shutdown
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from evals_mcp import auth
from evals_mcp.config import get_settings
from evals_mcp.errors import MissingApiKeyError
from evals_mcp.state import lifespan


@pytest.fixture
def creds_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    get_settings.cache_clear()
    return tmp_path / ".config" / "evals" / "credentials.json"


async def test_lifespan_boots_without_a_key(creds_path: Path) -> None:
    """Server must start even when no key is configured — the whole point of
    the lazy cache is that a mid-session `auth login` can recover the
    session."""
    _ = creds_path
    async with lifespan(None) as state:  # type: ignore[arg-type]
        assert state.platform is not None
        assert state.agent is not None


async def test_lifespan_caches_key_at_startup(creds_path: Path) -> None:
    """Both endpoints share the same ``Authorization: Bearer`` provider."""
    _ = creds_path
    auth.save_api_key("ak_initial")
    async with lifespan(None) as state:  # type: ignore[arg-type]
        h1 = state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]
        h2 = state.agent._headers_provider()  # pyright: ignore[reportPrivateUsage]
        assert h1 == {"Authorization": "Bearer ak_initial"}
        assert h2 == {"Authorization": "Bearer ak_initial"}


async def test_headers_provider_resolves_lazily_after_late_login(creds_path: Path) -> None:
    """User boots without a key, then logs in mid-session."""
    _ = creds_path
    async with lifespan(None) as state:  # type: ignore[arg-type]
        # Boot was unauthenticated; provider should still raise.
        with pytest.raises(MissingApiKeyError):
            state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]

        # User now logs in.
        auth.save_api_key("ak_late")
        headers = state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]
        assert headers == {"Authorization": "Bearer ak_late"}


async def test_headers_provider_picks_up_new_key_on_file_change(creds_path: Path) -> None:
    """Mid-session re-login must take effect on the very next request —
    the provider stat()s the credentials file each call and re-reads when
    inode/mtime have changed, so no explicit refresh hook is needed."""
    _ = creds_path
    auth.save_api_key("ak_old")
    async with lifespan(None) as state:  # type: ignore[arg-type]
        first = state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]
        assert first == {"Authorization": "Bearer ak_old"}

        auth.save_api_key("ak_new")
        refreshed = state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]
        assert refreshed == {"Authorization": "Bearer ak_new"}


async def test_headers_provider_detects_file_deletion(creds_path: Path) -> None:
    """After a successful read, a deleted credentials file must surface as
    MissingApiKeyError on the next call — never silently serve the stale
    cached headers, since the model needs to know to re-prompt for a key."""
    _ = creds_path
    auth.save_api_key("ak_old")
    async with lifespan(None) as state:  # type: ignore[arg-type]
        state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]  # populate cache

        auth.delete_api_key()
        with pytest.raises(MissingApiKeyError):
            state.platform._headers_provider()  # pyright: ignore[reportPrivateUsage]


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
