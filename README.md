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

Browser-based login via OAuth 2.0 + PKCE against Clerk (Pluto's identity
provider). Cross-platform — any OS, any browser.

**One-time setup:**
```bash
npx pluto-judge auth login
```

This opens your default browser to the Pluto login page. After you sign in,
tokens are stored at `~/.config/pluto/credentials.json` (mode `0600`) and
refreshed automatically. Other commands:

```bash
npx pluto-judge auth status   # Show who you're logged in as
npx pluto-judge auth logout   # Revoke and remove credentials
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
- Active Pluto account (https://pluto.plurai.ai)
