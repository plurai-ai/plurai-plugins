"""FastMCP server instance for the evals MCP server."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog
from mcp.server.fastmcp import FastMCP

from .config import get_settings
from .state import lifespan
from .tools import register_all

# ~1 MB per file across (1 active + 2 rotated) caps disk use near 3 MB.
# Sized for the "what just happened" tail-debugging case: ~5K recent
# INFO lines fit in 1 MB, more than enough context for a single tool
# call without unbounded growth over multi-day uptime.
_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUP_COUNT = 2


def _configure_logging() -> None:
    """Route structlog through stdlib logging with size-based rotation.

    MCP stdio reserves stdout for the protocol, and Claude Code discards
    server stderr from stdio servers — file logging is the only sink the
    user can ``tail -F``. ``RotatingFileHandler`` flushes on every emit
    (one record per write) so ``tail -F`` keeps up despite the absence of
    ``buffering=1`` on the underlying stream.
    """
    settings = get_settings()
    log_path = Path(settings.log_path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = RotatingFileHandler(
        log_path,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for noisy in ("httpx", "httpcore", "langgraph_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


_configure_logging()

mcp: FastMCP = FastMCP("plurai", lifespan=lifespan)
register_all(mcp)
