"""Settings + derived URL constants.

`Settings` is a pydantic-settings model that reads `PLUTO_*` env vars on
construction. The module-level aliases (`PLUTO_API`, `AGENT_API`, …) are
read off a single shared `settings` instance so callers don't need to know
about pydantic-settings.

To inject overrides in tests, instantiate `Settings(api_base=...)` directly
or set the corresponding env var before import.
"""

from __future__ import annotations

from functools import cached_property

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PLUTO_",
        case_sensitive=False,
        extra="ignore",
    )

    api_base: str = "https://pluto.stg.plurai.ai"
    # Public-facing dashboard / inference hosts. Default to the API host so
    # local/staging setups Just Work; override via env when they differ in
    # production (PLUTO_DASHBOARD_BASE, PLUTO_RUN_BASE).
    dashboard_base: str | None = None
    run_base: str | None = None

    @cached_property
    def pluto_api(self) -> str:
        return f"{self.api_base}/api/pluto"

    @cached_property
    def agent_api(self) -> str:
        return f"{self.api_base}/api/agent/api/copilotkit"

    @cached_property
    def dashboard_url(self) -> str:
        return (self.dashboard_base or self.api_base).rstrip("/")

    @cached_property
    def run_url(self) -> str:
        if self.run_base:
            return self.run_base.rstrip("/")
        # Mirror prod ↔ staging based on the API host.
        if "stg" in self.api_base:
            return "https://run.stg.plurai.ai"
        return "https://run.plurai.ai"


settings = Settings()

# Backwards-compatible module-level aliases. Imports stay terse:
#   from ..config import PLUTO_API, AGENT_API
PLUTO_API_BASE: str = settings.api_base
PLUTO_API: str = settings.pluto_api
AGENT_API: str = settings.agent_api
DASHBOARD_BASE: str = settings.dashboard_url
RUN_BASE: str = settings.run_url
