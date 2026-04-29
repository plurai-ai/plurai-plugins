# Pluto Judge — Claude Code MCP Server

Create fine-tuned LLM-as-a-judge evaluators directly from Claude Code.

## What it does

Type `/judge` and describe what you want to evaluate. The plugin will:
1. Create an evaluator on the Pluto platform
2. Present refinement questions via interactive UI
3. Optimize the evaluator (LLM or SLM)
4. Provide endpoint URL and API key

## Installation

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

Install as a Claude Code plugin:

```bash
/plugin install pluto-judge
```

(Or, until the plugin is published to a marketplace, install locally
from a clone — see [Local development](#local-development) below.)

The plugin ships its own `.mcp.json`, so once it's installed Claude
Code spawns the MCP server automatically. No manual `.mcp.json` edits.

## Authentication

Browser-based login via OAuth 2.0 + PKCE against Clerk (Pluto's identity
provider). Cross-platform — any OS, any browser.

**One-time setup** — once the plugin is installed, run inside any
Claude Code session:

```
/judge:login
```

That command opens your default browser to the Pluto login page. After
you sign in, tokens are stored at `~/.config/pluto/credentials.json`
(mode `0600`) and refreshed automatically.

## Usage

```
/judge I need to evaluate if my RAG responses are grounded in the context
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `pluto_search_evaluators` | Search existing evaluators (paginated) |
| `pluto_start_judge` | Create thread + send task + get refinement questions |
| `pluto_upload_data` | Upload labeled examples from a user-provided file |
| `pluto_send_message` | Send follow-up messages (answers, optimize) |
| `pluto_ask_user` | Present interactive questions to the user |
| `pluto_get_results` | Get optimization results (baseline vs optimized) |
| `pluto_create_api_key` | Generate an API key for the endpoint |

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `PLUTO_API_BASE` | `https://pluto.stg.plurai.ai` | API host (set to `https://pluto.plurai.ai` for prod) |
| `PLUTO_RUN_BASE` | (auto: prod ↔ staging) | Override the inference endpoint URL |
| `PLUTO_AUTH_METHOD` | `chrome` | `chrome` or `broker` (RFC 0001) |

## Local development

Working on the plugin source itself? The bundled
[.mcp.json](.mcp.json) uses `${CLAUDE_PLUGIN_ROOT}`, so it points at
whatever directory Claude Code treats as the plugin root. That gives you
a hot dev loop in two windows:

**Window A — editing window** (this repo, in your editor):
```bash
uv sync                  # one-time: install deps + editable package
uv run pytest            # tests
uv run ruff check .      # lint
uv run pyright           # type-check
./dev-restart.sh         # kill running MCP server processes after edits
```

**Window B — test window** (a different folder, e.g. some scratch project):
```bash
claude --plugin-dir /absolute/path/to/pluto-judge
```

Claude treats the local checkout as if it were an installed plugin and
sets `CLAUDE_PLUGIN_ROOT` to that path. The bundled
[.mcp.json](.mcp.json) runs the server with
`uv run --project ${CLAUDE_PLUGIN_ROOT} python -m pluto_judge`. Because
`[tool.uv] package = true` is set in
[pyproject.toml](pyproject.toml), uv installs the project **editably**
into `${CLAUDE_PLUGIN_ROOT}/.venv` — every server spawn imports your
live source files. (We tried `uvx --from <path>` first; it caches an
old wheel and silently keeps serving stale code, so it's the wrong tool
for active development.)

After editing source in Window A:

1. Window A: `./dev-restart.sh`
2. Window B: `/mcp` → `pluto-judge` → `Reconnect`
3. Next tool call spawns a fresh server from your edited code. No
   rebuild step — the editable install reads from `src/pluto_judge/`
   directly.

VSCode users: [.vscode/settings.json](.vscode/settings.json) points
Pylance at the local `.venv`. After `uv sync`, run
`Cmd-Shift-P → Python: Select Interpreter → ./.venv/bin/python` once and
the import squiggles disappear.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Active Pluto account (https://pluto.plurai.ai)
