#!/bin/bash
set -e

# Pluto Judge — Claude Code Plugin Installer
# Installs both the /judge command and the MCP server tools

REPO_URL="git@github.com:plurai-ai/pluto-judge.git"

# ── 1. Find python3 ──────────────────────────────────────────────────────
PYTHON3=$(which python3 2>/dev/null || true)
if [ -z "$PYTHON3" ]; then
    echo "Error: python3 not found. Install Python 3.10+ first."
    exit 1
fi
echo "Found python3: $PYTHON3"

# ── 2. Clone plugin to Claude's plugin cache ─────────────────────────────
PLUGIN_DIR="$HOME/.claude/plugins/cache/local/pluto-judge/0.1.0"
if [ -d "$PLUGIN_DIR" ]; then
    echo "Updating existing plugin..."
    cd "$PLUGIN_DIR" && git pull 2>/dev/null || true
else
    echo "Installing plugin..."
    git clone "$REPO_URL" "$PLUGIN_DIR"
fi
echo "Plugin installed at: $PLUGIN_DIR"

# ── 3. Register plugin in installed_plugins.json ─────────────────────────
PLUGINS_FILE="$HOME/.claude/plugins/installed_plugins.json"
if [ -f "$PLUGINS_FILE" ]; then
    # Check if already registered
    if ! python3 -c "import json; d=json.load(open('$PLUGINS_FILE')); exit(0 if 'pluto-judge@local' in d.get('plugins',{}) else 1)" 2>/dev/null; then
        python3 -c "
import json
with open('$PLUGINS_FILE') as f:
    d = json.load(f)
d.setdefault('plugins', {})['pluto-judge@local'] = [{
    'scope': 'user',
    'installPath': '$PLUGIN_DIR',
    'version': '0.1.0',
    'installedAt': '$(date -u +%Y-%m-%dT%H:%M:%S.000Z)',
    'lastUpdated': '$(date -u +%Y-%m-%dT%H:%M:%S.000Z)'
}]
with open('$PLUGINS_FILE', 'w') as f:
    json.dump(d, f, indent=2)
"
        echo "Plugin registered"
    else
        echo "Plugin already registered"
    fi
fi

# ── 4. Add MCP server to project .mcp.json ───────────────────────────────
PROJECT_DIR="${1:-.}"
MCP_FILE="$PROJECT_DIR/.mcp.json"

if [ -f "$MCP_FILE" ]; then
    # Check if pluto-judge already configured
    if python3 -c "import json; d=json.load(open('$MCP_FILE')); exit(0 if 'pluto-judge' in d.get('mcpServers',{}) else 1)" 2>/dev/null; then
        echo "MCP server already in $MCP_FILE"
    else
        echo ""
        echo "Add this to $MCP_FILE under mcpServers:"
        echo "    \"pluto-judge\": {"
        echo "        \"command\": \"$PYTHON3\","
        echo "        \"args\": [\"src/server.py\"],"
        echo "        \"cwd\": \"$PLUGIN_DIR\""
        echo "    }"
    fi
else
    cat > "$MCP_FILE" << MCPEOF
{
    "mcpServers": {
        "pluto-judge": {
            "command": "$PYTHON3",
            "args": ["src/server.py"],
            "cwd": "$PLUGIN_DIR"
        }
    }
}
MCPEOF
    echo "Created $MCP_FILE"
fi

# ── 5. Enable MCP server in project settings ─────────────────────────────
SETTINGS_DIR="$PROJECT_DIR/.claude"
SETTINGS_FILE="$SETTINGS_DIR/settings.json"
mkdir -p "$SETTINGS_DIR"

if [ -f "$SETTINGS_FILE" ]; then
    if ! grep -q "enableAllProjectMcpServers" "$SETTINGS_FILE" 2>/dev/null; then
        echo ""
        echo "Add to $SETTINGS_FILE:"
        echo '    "enableAllProjectMcpServers": true,'
        echo '    "enabledMcpjsonServers": ["pluto-judge"],'
        echo '    "enabledPlugins": { "pluto-judge@local": true }'
    fi
else
    cat > "$SETTINGS_FILE" << SETEOF
{
    "enableAllProjectMcpServers": true,
    "enabledMcpjsonServers": ["pluto-judge"],
    "enabledPlugins": {
        "pluto-judge@local": true
    }
}
SETEOF
    echo "Created $SETTINGS_FILE"
fi

# ── 6. Auth setup ────────────────────────────────────────────────────────
echo ""
echo "── Auth Setup ──"
if [ -f "$HOME/.config/pluto/credentials.json" ]; then
    echo "Credentials already present at ~/.config/pluto/credentials.json"
    echo "(Run \`npx pluto-judge auth status\` to inspect.)"
else
    echo "Run this once to log in via your browser:"
    echo "  npx pluto-judge auth login"
fi

echo ""
echo "── Done! ──"
echo "1. Run: npx pluto-judge auth login"
echo "2. Restart Claude Code"
echo "3. Type: /judge"
