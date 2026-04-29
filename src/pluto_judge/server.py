"""FastMCP server instance for pluto-judge."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .state import lifespan
from .tools import register_all

mcp: FastMCP = FastMCP("pluto-judge", lifespan=lifespan)
register_all(mcp)
