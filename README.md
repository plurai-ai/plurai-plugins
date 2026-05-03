# Evals — Claude Code MCP Server

Create fine-tuned LLM-as-a-judge evaluators on the [Plurai platform](https://plurai.ai), directly from Claude Code.

## What it does

Type `/eval` and describe what you want to evaluate. The plugin will:
1. Create an evaluator on the Plurai platform
2. Present refinement questions via interactive UI
3. Optimize the evaluator (LLM or SLM)
4. Provide an endpoint URL and API key

## Installation

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```
/plugin marketplace add plurai-ai/plurai-plugins-official
/plugin install evals@plurai-plugins-official
```

The plugin ships its own `.mcp.json`, so once it's installed Claude Code spawns the MCP server automatically. No manual `.mcp.json` edits.

## Authentication

The plugin authenticates with a long-lived API key issued from your Plurai account. Sign in once per machine:

```
/login
```

The slash command prompts for your key, then runs `evals-mcp auth login --key <KEY>`, which writes it to `~/.config/evals/credentials.json` (mode `0600`). Override the path with `EVALS_CREDENTIALS_PATH`, or skip the file entirely by exporting `EVALS_API_KEY` in your environment.

## Usage

```
/eval I need to evaluate if my RAG responses are grounded in the context
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `evals_search_evaluators` | Search existing evaluators (paginated) |
| `evals_start_judge` | Create thread + send task + get refinement questions |
| `evals_upload_data` | Upload labeled examples from a user-provided file |
| `evals_send_message` | Send follow-up messages (answers, optimize) |
| `evals_ask_user` | Present interactive questions to the user |
| `evals_get_results` | Get optimization results (baseline vs optimized) |
| `evals_create_api_key` | Generate an API key for the endpoint |

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `EVALS_API_KEY` | (unset) | API key. Wins over the credentials file when set. |
| `EVALS_CREDENTIALS_PATH` | `~/.config/evals/credentials.json` | Credentials file path |
| `EVALS_API_BASE` | `https://pluto.stg.plurai.ai` | API host (set to `https://pluto.plurai.ai` for prod) |
| `EVALS_RUN_BASE` | (auto: prod ↔ staging) | Override the inference endpoint URL |
| `EVALS_HTTP_TIMEOUT` | `30` | Per-request timeout for JSON calls (seconds) |
| `EVALS_AGENT_HTTP_TIMEOUT` | `300` | Timeout for the long-running agent SSE stream (seconds) |
| `EVALS_HTTP_MAX_RETRIES` | `3` | Retries on retryable HTTP failures (5xx/429/408/transport) |

## Local development

Working on the plugin source itself? The bundled [.mcp.json](.mcp.json) uses `${CLAUDE_PLUGIN_ROOT}`, so it points at whatever directory Claude Code treats as the plugin root. That gives you a hot dev loop in two windows:

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
claude --plugin-dir /absolute/path/to/plurai-plugins-official
```

Claude treats the local checkout as if it were an installed plugin and sets `CLAUDE_PLUGIN_ROOT` to that path. The bundled [.mcp.json](.mcp.json) runs the server with `uv run --project ${CLAUDE_PLUGIN_ROOT} python -m evals_mcp`. Because `[tool.uv] package = true` is set in [pyproject.toml](pyproject.toml), uv installs the project **editably** into `${CLAUDE_PLUGIN_ROOT}/.venv` — every server spawn imports your live source files. (We tried `uvx --from <path>` first; it caches an old wheel and silently keeps serving stale code, so it's the wrong tool for active development.)

After editing source in Window A:

1. Window A: `./dev-restart.sh`
2. Window B: `/mcp` → `evals` → `Reconnect`
3. Next tool call spawns a fresh server from your edited code. No rebuild step — the editable install reads from `src/evals_mcp/` directly.

VSCode users: [.vscode/settings.json](.vscode/settings.json) points Pylance at the local `.venv`. After `uv sync`, run `Cmd-Shift-P → Python: Select Interpreter → ./.venv/bin/python` once and the import squiggles disappear.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Active Plurai account (https://pluto.plurai.ai)
