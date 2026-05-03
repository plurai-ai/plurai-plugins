#!/usr/bin/env bash
# Dev helper: kill any lingering evals MCP server processes so the
# next /mcp reconnect (or new Claude Code session) spawns a fresh one
# with the latest code. Python doesn't hot-reload modules, so a server
# that started before your code change keeps the old code in memory
# forever — running this is the cleanest reset.
#
# Usage:
#   ./dev-restart.sh          # kill all running evals MCP server processes
#
# Then in Claude Code: /mcp → evals → Reconnect.

set -e

PIDS=$(pgrep -f "evals_mcp" || true)

if [ -z "$PIDS" ]; then
    echo "No running evals MCP server processes."
    exit 0
fi

echo "Killing evals MCP server processes:"
ps -o pid,etime,command -p $PIDS

# TERM first; SIGKILL anything that doesn't exit within 2s.
kill $PIDS 2>/dev/null || true
sleep 2
STILL=$(pgrep -f "evals_mcp" || true)
if [ -n "$STILL" ]; then
    echo "Force-killing stragglers: $STILL"
    kill -9 $STILL 2>/dev/null || true
fi

echo
echo "Done. In Claude Code: /mcp → evals → Reconnect."
