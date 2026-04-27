# Pluto-Judge — Claude Code Plugin

A Claude Code plugin that ships <commands / skills / an MCP server> for creating evaluator agents.

## Repo layout
- `.claude-plugin/plugin.json` — manifest (bump `version` for releases)
- `commands/` — slash commands shipped to users (one .md per command)
- `skills/<name>/SKILL.md` — skills shipped to users (frontmatter required)
- `src/<pkg>/` — Python MCP server (FastMCP, registered via `.mcp.json`)
- `evals/` — MCP tool evaluations (realistic multi-step questions)
- `tests/` — pytest unit + integration tests

## Authoring guidance (on-demand skills)
- For MCP server design (tool naming, annotations, transports, pagination,
  Pydantic patterns, FastMCP usage), use the `mcp-builder` skill.
- For plugin packaging (plugin.json schema, command frontmatter, skill
  frontmatter, hooks, marketplace.json), use the `plugin-development` skill
  if installed; otherwise consult docs at code.claude.com/docs/en/plugins.

## Stack
- Python 3.11+, `uv` for env/deps
- FastMCP (MCP Python SDK), Pydantic v2
  (`ConfigDict(extra='forbid', str_strip_whitespace=True)`)
- `httpx.AsyncClient` for outbound HTTP
- `ruff` + `ruff format`, `mypy --strict`
- `pytest` + `pytest-asyncio`

## Commands
- Install dev deps: `uv sync`
- Lint: `uv run ruff check . && uv run ruff format --check .`
- Tests: `uv run pytest`
- Eval suite: `uv run python evals/run.py`
- Local plugin test: `claude --plugin-dir .` then `/reload-plugins` after edits

## House conventions
### MCP tools (in `src/<pkg>/tools/`)
- All tool names prefixed `<svc>_` (e.g. `acme_search_users`)
- Every `@mcp.tool` sets `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`
- Pydantic input models with strict `ConfigDict`
- Default response format Markdown; accept `response_format: "json" | "markdown"` for data-heavy tools
- Always paginate list endpoints

### Slash commands (in `commands/`)
- Filename = command name (`commands/foo.md` → `/<plugin>:foo`)
- Frontmatter: `description`, `argument-hint`
- Use gerund forms in skill names (`git-pushing`, not `git-push`)

### Skills (in `skills/<name>/SKILL.md`)
- Frontmatter `description` is critical — it's what triggers activation
- Process steps go in `SKILL.md`; reference material in `skills/<name>/reference/*.md`
- Don't dump background context into `SKILL.md` — keep it procedural

## Release checklist
1. Bump `version` in `.claude-plugin/plugin.json`
2. Run `uv run pytest && uv run python evals/run.py`
3. Update `README.md` and `CHANGELOG.md`
4. Tag and push; marketplace consumers pull from the tag

## Don't
- Don't put `commands/`, `skills/`, `agents/`, or `hooks/` inside `.claude-plugin/` — only `plugin.json` lives there
- Don't run the MCP server with `python -m <pkg>` outside stdio context — it hangs
- Don't ship CLAUDE.md content to users — it's not loaded from installed plugins