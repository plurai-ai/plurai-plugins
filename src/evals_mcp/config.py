"""Settings singleton for the evals MCP server.

`Settings` is a pydantic-settings model that reads `EVALS_*` env vars on
construction. Use `get_settings()` for the lazily-initialised, cached
instance — every caller shares the same object.

For test overrides: instantiate `Settings(api_base=...)` directly, or set
the env var before the first `get_settings()` call.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .clients import BaseHttpClientConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EVALS_",
        case_sensitive=False,
        extra="ignore",
    )

    api_base: str = Field(default="https://app.plurai.ai", min_length=1)
    run_base: str = Field(default="https://run.plurai.ai", min_length=1)

    # Where the user's API key is persisted by the `auth login` CLI.
    # Resolved with ``Path.expanduser()`` at use-site, so ``~`` works.
    credentials_path: str = "~/.config/evals/credentials.json"

    # Where structured server logs are written. Claude Code discards stderr
    # from stdio MCP servers, so file logging is the only tail-able sink.
    log_path: str = "~/.cache/evals-mcp/server.log"
    log_level: str = "INFO"

    # HTTP client knobs (env: EVALS_HTTP_TIMEOUT, EVALS_HTTP_MAX_RETRIES, ...).
    http_timeout: float = Field(default=30.0, gt=0)
    http_max_retries: int = Field(default=3, ge=0)
    http_backoff_base: float = Field(default=1.0, gt=0)
    http_backoff_max: float = Field(default=30.0, gt=0)
    agent_http_timeout: float = Field(default=300.0, gt=0)
    langgraph_assistant_id: str = "calibration_agent"
    classifier_wait_timeout_s: float = Field(default=120.0, gt=0)

    # When unset (the default), filled by ``_derive_urls`` from ``api_base``.
    platform_api: str = ""
    langgraph_url: str = ""

    _DEFAULT_LANGGRAPH_URL = "https://api.plurai.ai/pluto/agent/langgraph"

    @model_validator(mode="after")
    def _derive_urls(self) -> Settings:
        self.api_base = self.api_base.rstrip("/")
        self.run_base = self.run_base.rstrip("/")
        if not self.platform_api:
            self.platform_api = f"{self.api_base}/api/pluto"
        if not self.langgraph_url:
            self.langgraph_url = self._DEFAULT_LANGGRAPH_URL
        # Normalize after both derivation and explicit-override paths so an
        # ``EVALS_LANGGRAPH_URL`` with a trailing slash doesn't produce
        # double-slash URLs downstream.
        self.platform_api = self.platform_api.rstrip("/")
        self.langgraph_url = self.langgraph_url.rstrip("/")
        if not self.platform_api or not self.langgraph_url:
            raise ValueError("platform_api / langgraph_url cannot be empty after derivation")
        return self

    def platform_client_config(self) -> BaseHttpClientConfig:
        return BaseHttpClientConfig(
            api_url=self.platform_api,
            timeout=self.http_timeout,
            max_retries=self.http_max_retries,
            backoff_base=self.http_backoff_base,
            backoff_max=self.http_backoff_max,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
