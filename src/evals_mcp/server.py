"""FastMCP server instance for the evals MCP server."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .state import lifespan
from .tools import register_all

mcp: FastMCP = FastMCP("evals", lifespan=lifespan)
register_all(mcp)
