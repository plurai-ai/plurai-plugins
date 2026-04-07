# Pluto Judge — Claude Code Plugin

Create fine-tuned LLM-as-a-judge evaluators directly from Claude Code.

## What it does

Say `/judge` and describe what you want to evaluate. The plugin will:
1. Create an evaluator on the Pluto platform
2. Present refinement questions via interactive UI
3. Optimize the evaluator (LLM or SLM)
4. Provide endpoint URL and API key

## Installation

```bash
# Clone into your project
git clone git@github.com:plurai-ai/pluto-judge.git

# Find your python3 absolute path (required for VS Code)
which python3

# Create .mcp.json in your project root (replace python path)
cat > .mcp.json << 'EOF'
{
    "mcpServers": {
        "pluto-judge": {
            "command": "/opt/homebrew/bin/python3",
            "args": ["server.py"],
            "cwd": "./pluto-judge"
        }
    }
}
EOF

# Approve the MCP server in .claude/settings.json
# Add: "enableAllProjectMcpServers": true
```

## Authentication

The plugin reads your active Pluto session from Chrome cookies (macOS only).

**Setup:**
1. Log in at https://pluto.plurai.ai in Chrome
2. Set your Chrome Safe Storage key:
   ```bash
   # Get the key (one-time, will prompt for keychain access)
   security find-generic-password -w -s "Chrome Safe Storage" -a "Chrome"
   
   # Add to your shell profile
   export CHROME_SAFE_STORAGE="<key from above>"
   ```

## Usage

```
/judge I need to evaluate if my RAG responses are grounded in the context
```

Or with existing labeled data:

```
/judge --data grounding_examples.csv
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `pluto_start_judge` | Create thread + send task + get refinement questions |
| `pluto_upload_data` | Upload labeled examples from a user-provided file |
| `pluto_send_message` | Send follow-up messages (answers, optimize) |
| `pluto_ask_user` | Present interactive questions to the user |
| `pluto_get_results` | Get optimization results (baseline vs optimized) |
| `pluto_create_api_key` | Generate an API key for the endpoint |

## Requirements

- Python 3.10+ (stdlib only, zero dependencies)
- macOS with Chrome (for cookie-based auth)
- Active Pluto account (https://pluto.plurai.ai)

## Troubleshooting

**MCP tools not showing up?**
1. Ensure `.mcp.json` uses the **absolute path** to python3
2. Add `"enableAllProjectMcpServers": true` to `.claude/settings.json`
3. Restart Claude Code after config changes

**Authentication errors?**
1. Log in to https://pluto.plurai.ai in Chrome
2. Ensure `CHROME_SAFE_STORAGE` env var is set
