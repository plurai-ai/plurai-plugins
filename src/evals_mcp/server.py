"""FastMCP server instance for the evals MCP server."""

from __future__ import annotations

import logging
from pathlib import Path

import structlog
from mcp.server.fastmcp import FastMCP

from .config import get_settings
from .state import lifespan
from .tools import register_all


def _configure_logging() -> None:
    """Route structlog to a file. MCP stdio reserves stdout for the protocol,
    and Claude Code discards server stderr from stdio servers — file logging
    is the only sink the user can ``tail -F``."""
    settings = get_settings()
    log_path = Path(settings.log_path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", buffering=1)  # line-buffered for tail -F

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=log_file),
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


_configure_logging()

mcp: FastMCP = FastMCP("evals", lifespan=lifespan)
register_all(mcp)
