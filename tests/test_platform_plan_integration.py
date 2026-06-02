"""Opt-in integration test for GET /plan against the real Pluto API.

Skipped unless the user has a real API key at
``~/.config/evals/credentials.json``. Run with::

    uv run pytest -m integration

The default ``uv run pytest`` excludes the ``integration`` marker via
``addopts`` in ``pyproject.toml`` so the regular suite stays hermetic.
"""

from __future__ import annotations

import pytest

from evals_mcp.auth.auth import BearerCache, load_api_key
from evals_mcp.clients import PlatformClient
from evals_mcp.config import get_settings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_plan_against_real_pluto_api() -> None:
    """Hit the real Pluto API's GET /plan using the on-disk credentials.

    Verifies the response shape this plugin relies on for the SLM gate:
    ``id`` is a known plan tier, and the entitlements block exposes the
    boolean ``slm_endpoints`` / ``llm_endpoints`` fields.
    """
    try:
        key = load_api_key()
    except Exception as e:  # CorruptCredentialsError or transient OS error
        pytest.skip(f"credentials present but unreadable: {e}")
    if not key:
        pytest.skip("no API key configured at ~/.config/evals/credentials.json")

    settings = get_settings()
    bearer = BearerCache()
    async with PlatformClient(
        settings.platform_client_config(),
        headers_provider=bearer.headers,
    ) as platform:
        plan = await platform.get_plan()

    assert plan.id in {"free", "paid", "enterprise"}, f"unexpected plan id: {plan.id!r}"
    assert isinstance(plan.entitlements.slm_endpoints, bool)
    assert isinstance(plan.entitlements.llm_endpoints, bool)
