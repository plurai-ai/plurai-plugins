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

Two backends, selected by `PLUTO_AUTH_METHOD`:

- **`chrome` (default, macOS only)** — reads your existing Pluto session
  straight from the local Chrome cookie store and exchanges it for a JWT
  via Clerk. No browser flow, no file-based credential store; if you're
  already signed in to Pluto in Chrome it just works. Requires `openssl`
  on `PATH` and reads the Chrome safe-storage seed from the macOS
  keychain (override with `CHROME_SAFE_STORAGE` for non-keychain setups).

- **`broker` (cross-platform)** — browser-based OAuth 2.0 + PKCE flow
  against Clerk. Tokens are written to
  `~/.config/pluto/credentials.json` (mode `0600`, override with
  `PLUTO_CREDENTIALS_PATH`) and refreshed automatically. Use this on
  Linux/Windows or when you don't have a usable Chrome session.

**Sign in** — once the plugin is installed, from any Claude Code session:

```
/pluto-judge:login
```

In `chrome` mode the command verifies the cached cookie/JWT and prints
who you're signed in as; in `broker` mode it opens your default browser
to the Pluto login page and waits for you to complete sign-in.

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
| `PLUTO_AUTH_METHOD` | `chrome` | `chrome` (macOS, default) or `broker` (RFC 0001, cross-platform) |
| `PLUTO_CREDENTIALS_PATH` | `~/.config/pluto/credentials.json` | Broker-only credentials file path |
| `CHROME_SAFE_STORAGE` | (read from macOS keychain) | Chrome-only safe-storage seed override |
| `PLUTO_HTTP_TIMEOUT` | `30` | Per-request timeout for Pluto JSON calls (seconds) |
| `PLUTO_AGENT_HTTP_TIMEOUT` | `300` | Timeout for the long-running agent SSE stream (seconds) |
| `PLUTO_HTTP_MAX_RETRIES` | `3` | Retries on retryable HTTP failures (5xx/429/408/transport) |

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
