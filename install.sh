#!/bin/bash
# Evals MCP — install via uv tool.
# Requires uv (https://docs.astral.sh/uv/). Installs the evals-mcp
# console script onto your PATH and reminds you to authenticate.

set -e

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required. Install it: https://docs.astral.sh/uv/"
    exit 1
fi

REPO_URL="${EVALS_MCP_REPO:-git+https://github.com/plurai-ai/plurai-plugins-official.git}"

uv tool install --force "$REPO_URL"

echo
echo "Installed. Now run:"
echo "  evals-mcp auth login --key <KEY>"
