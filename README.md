# Pluto Judge — Claude Code MCP Server

Create fine-tuned LLM-as-a-judge evaluators directly from Claude Code.

## What it does

Type `/judge` and describe what you want to evaluate. The plugin will:
1. Create an evaluator on the Pluto platform
2. Present refinement questions via interactive UI
3. Optimize the evaluator (LLM or SLM)
4. Provide endpoint URL and API key

## Installation

Add to your project's `.mcp.json`:

```json
{
    "mcpServers": {
        "pluto-judge": {
            "command": "npx",
            "args": ["-y", "pluto-judge"]
        }
    }
}
```

That's it. Requires Node.js 16+ and Python 3.10+.

## Authentication

The server reads your active Pluto session from Chrome cookies (macOS).

**One-time setup:**
```bash
# 1. Log in at https://pluto.plurai.ai in Chrome

# 2. Get your Chrome Safe Storage key
security find-generic-password -w -s "Chrome Safe Storage" -a "Chrome"

# 3. Add to shell profile (~/.zshrc)
export CHROME_SAFE_STORAGE="<key from step 2>"
```

## Usage

```
/judge I need to evaluate if my RAG responses are grounded in the context
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

- Node.js 16+ (for npx)
- Python 3.10+ (stdlib only, zero Python dependencies)
- macOS with Chrome (for cookie-based auth)
- Active Pluto account (https://pluto.plurai.ai)
