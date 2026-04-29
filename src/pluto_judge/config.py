"""Settings singleton for pluto-judge.

`Settings` is a pydantic-settings model that reads `PLUTO_*` env vars on
construction. Use `get_settings()` for the lazily-initialised, cached
instance — every caller shares the same object.

For test overrides: instantiate `Settings(api_base=...)` directly, or set
the env var before the first `get_settings()` call.
"""

from __future__ import annotations

from functools import cached_property, lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .clients import BaseHttpClientConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PLUTO_",
        case_sensitive=False,
        extra="ignore",
    )

    api_base: str = "https://pluto.stg.plurai.ai"
    run_base: str | None = None

    # HTTP client knobs (env: PLUTO_HTTP_TIMEOUT, PLUTO_HTTP_MAX_RETRIES, ...).
    http_timeout: float = Field(default=30.0, gt=0)
    http_max_retries: int = Field(default=3, ge=0)
    http_backoff_base: float = Field(default=1.0, gt=0)
    http_backoff_max: float = Field(default=30.0, gt=0)
    # Agent (CopilotKit SSE) timeout is separate: streams legitimately run
    # for minutes, so the JSON-request default (30s) is too short.
    agent_http_timeout: float = Field(default=300.0, gt=0)

    @cached_property
    def pluto_api(self) -> str:
        return f"{self.api_base}/api/pluto"

    @cached_property
    def agent_api_base(self) -> str:
        """Base URL for the CopilotKit agent endpoint (no trailing path).

        Used as the ``api_url`` for ``AgentClient`` so requests can supply
        ``/copilotkit`` as the path. Posting to a base_url with an empty
        path makes httpx normalise to a trailing slash, which the agent
        backend rejects with a 404.
        """
        return f"{self.api_base}/api/agent/api"

    @cached_property
    def agent_api(self) -> str:
        return f"{self.agent_api_base}/copilotkit"

    @cached_property
    def run_url(self) -> str:
        if self.run_base:
            return self.run_base.rstrip("/")
        # Mirror prod ↔ staging based on the API host.
        if "stg" in self.api_base:
            return "https://run.stg.plurai.ai"
        return "https://run.plurai.ai"

    def pluto_client_config(self) -> BaseHttpClientConfig:
        return BaseHttpClientConfig(
            api_url=self.pluto_api,
            timeout=self.http_timeout,
            max_retries=self.http_max_retries,
            backoff_base=self.http_backoff_base,
            backoff_max=self.http_backoff_max,
        )

    def agent_client_config(self) -> BaseHttpClientConfig:
        # SSE: tenacity retry is skipped at the streaming layer anyway, but
        # keep transient retries off for the underlying httpx client too.
        return BaseHttpClientConfig(
            api_url=self.agent_api_base,
            timeout=self.agent_http_timeout,
            max_retries=0,
            backoff_base=self.http_backoff_base,
            backoff_max=self.http_backoff_max,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
