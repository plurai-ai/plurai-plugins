"""Tool registration for the FastMCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import classifiers, data, judge

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register_all(mcp: FastMCP) -> None:
    """Register every tool defined in this package on the given FastMCP instance."""
    judge.register(mcp)
    data.register(mcp)
    classifiers.register(mcp)
