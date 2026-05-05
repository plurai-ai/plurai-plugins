"""Settings URL derivation: trailing-slash normalization + non-empty guard."""

from __future__ import annotations

import pytest

from evals_mcp.config import Settings


def test_trailing_slashes_are_stripped_on_all_urls() -> None:
    """Without normalization, an ``api_base`` with a trailing slash produces
    ``//api/pluto`` URLs; same for the run base in tool responses."""
    s = Settings(
        api_base="https://app.example/",
        run_base="https://run.example/",
    )
    assert s.api_base == "https://app.example"
    assert s.run_base == "https://run.example"
    assert s.platform_api == "https://app.example/api/pluto"


def test_explicit_langgraph_url_override_is_normalized() -> None:
    s = Settings(langgraph_url="https://staging-langgraph.example/path/")
    assert s.langgraph_url == "https://staging-langgraph.example/path"


def test_explicit_platform_api_override_is_normalized() -> None:
    s = Settings(platform_api="https://x/api/pluto/")
    assert s.platform_api == "https://x/api/pluto"


def test_explicit_run_base_override_wins() -> None:
    """``EVALS_RUN_BASE`` (or constructor arg) must beat the default so
    users can point at a custom environment."""
    s = Settings(run_base="https://run.custom.example")
    assert s.run_base == "https://run.custom.example"


def test_empty_api_base_is_rejected() -> None:
    """An empty ``api_base`` would derive empty platform/langgraph URLs and
    silently produce localhost requests downstream — fail fast at config."""
    with pytest.raises(ValueError):
        Settings(api_base="")
