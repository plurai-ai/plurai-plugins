"""Tests for the lifespan auth integration in state.py.

Covers the cache lifecycle that links auth.py to the HTTP clients:
- startup with no key must not crash (so /pluto-judge:login can recover)
- ``headers_provider`` lazily resolves on first call
- ``auth_refresh`` re-reads after the user logs in
- a failed refresh clears the cache instead of serving stale headers
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pluto_judge import auth
from pluto_judge.errors import MissingApiKeyError
from pluto_judge.state import lifespan


@pytest.fixture
def creds_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "pluto" / "credentials.json"
    monkeypatch.setenv("PLUTO_CREDENTIALS_PATH", str(path))
    monkeypatch.delenv("PLUTO_API_KEY", raising=False)
    return path


async def test_lifespan_boots_without_a_key(
    creds_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Server must start even when no key is configured — the whole point of
    the lazy cache is that ``/pluto-judge:login`` can recover the session."""
    _ = creds_path
    async with lifespan(None) as state:  # type: ignore[arg-type]
        assert state.pluto is not None
        assert state.agent is not None
    err = capsys.readouterr().err
    assert "no API key at startup" in err


async def test_lifespan_caches_key_at_startup(
    creds_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUTO_API_KEY", "ak_initial")
    async with lifespan(None) as state:  # type: ignore[arg-type]
        # Both clients must share the same headers_provider closure so
        # auth_refresh updates them in lockstep.
        h1 = await state.pluto._headers_provider()  # noqa: SLF001
        h2 = await state.agent._headers_provider()  # noqa: SLF001
        assert h1 == h2 == {"Authorization": "Bearer ak_initial"}


async def test_headers_provider_resolves_lazily_after_late_login(
    creds_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User boots without a key, then runs /pluto-judge:login mid-session."""
    async with lifespan(None) as state:  # type: ignore[arg-type]
        # Boot was unauthenticated; provider should still raise.
        with pytest.raises(MissingApiKeyError):
            await state.pluto._headers_provider()  # noqa: SLF001

        # User now logs in (or sets the env var).
        monkeypatch.setenv("PLUTO_API_KEY", "ak_late")
        headers = await state.pluto._headers_provider()  # noqa: SLF001
        assert headers == {"Authorization": "Bearer ak_late"}


async def test_auth_refresh_picks_up_new_key(
    creds_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUTO_API_KEY", "ak_old")
    async with lifespan(None) as state:  # type: ignore[arg-type]
        first = await state.pluto._headers_provider()  # noqa: SLF001
        assert first == {"Authorization": "Bearer ak_old"}

        monkeypatch.setenv("PLUTO_API_KEY", "ak_new")
        # Without auth_refresh, the cache would still hand back ak_old.
        await state.pluto._auth_refresh()  # type: ignore[misc]  # noqa: SLF001
        refreshed = await state.pluto._headers_provider()  # noqa: SLF001
        assert refreshed == {"Authorization": "Bearer ak_new"}


async def test_auth_refresh_failure_clears_cache(
    creds_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If refresh fails, subsequent requests must not serve the previously
    cached (now-revoked) headers — they should fail loudly so the model
    prompts re-login instead of silently using a dead key."""
    monkeypatch.setenv("PLUTO_API_KEY", "ak_old")
    async with lifespan(None) as state:  # type: ignore[arg-type]
        await state.pluto._headers_provider()  # noqa: SLF001  # populate cache

        monkeypatch.delenv("PLUTO_API_KEY")
        auth.delete_api_key()
        with pytest.raises(MissingApiKeyError):
            await state.pluto._auth_refresh()  # type: ignore[misc]  # noqa: SLF001

        with pytest.raises(MissingApiKeyError):
            await state.pluto._headers_provider()  # noqa: SLF001
