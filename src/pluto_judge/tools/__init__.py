"""Tool registration for the FastMCP server."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import classifiers, data, judge


def register_all(mcp: FastMCP) -> None:
    """Register every tool defined in this package on the given FastMCP instance."""
    judge.register(mcp)
    data.register(mcp)
    classifiers.register(mcp)
