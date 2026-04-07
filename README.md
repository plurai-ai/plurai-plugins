# Pluto Judge — Claude Code Plugin

Create fine-tuned LLM-as-a-judge evaluators directly from Claude Code.

## What it does

Say `/judge` and describe what you want to evaluate. The plugin will:
1. Create an evaluator on the Pluto platform
2. Ask a few refinement questions
3. Generate synthetic test data
4. Optimize the evaluator (LLM or SLM)
5. Integrate the endpoint into your code

## Quick Install

```bash
curl -sL https://raw.githubusercontent.com/plurai-ai/pluto-judge/main/install.sh | bash
```

Or manually:

```bash
# 1. Clone into your project
git clone https://github.com/plurai-ai/pluto-judge.git

# 2. Find your python3 absolute path (required for VS Code)
which python3
# e.g. /opt/homebrew/bin/python3

# 3. Create .mcp.json in your project root
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
# ⚠️  Replace the command path with YOUR python3 path from step 2

# 4. Approve the MCP server in .claude/settings.json
# Add: "enableAllProjectMcpServers": true

# 5. Restart Claude Code
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

Once connected, these tools are available:

| Tool | Description |
|------|-------------|
| `pluto_create_thread` | Create a new evaluator thread |
| `pluto_upload_data` | Upload labeled examples |
| `pluto_send_message` | Chat with the Pluto agent |
| `pluto_get_results` | Get optimization results |
| `pluto_create_api_key` | Generate an API key for the endpoint |

## Requirements

- Python 3.10+ (stdlib only, zero dependencies)
- Active Pluto account (https://pluto.plurai.ai)

## Troubleshooting

**MCP tools not showing up?**

1. Check that `.mcp.json` uses the **absolute path** to python3 (not just `python3`)
2. Ensure `.claude/settings.json` has `"enableAllProjectMcpServers": true`
3. Restart Claude Code after any config changes
