# Evals — Claude Code MCP Server

Create fine-tuned LLM-as-a-judge evaluators on the [Plurai platform](https://plurai.ai), directly from Claude Code. Describe what you want to evaluate, answer a few refinement questions, and the plugin returns a deployed HTTPS endpoint you can call from your application.

## Requirements

- [Claude Code](https://docs.claude.com/claude-code)
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- A free [Plurai account](https://app.plurai.ai)

## Install

In any Claude Code session:

```
/plugin marketplace add plurai-ai/plurai-plugins
/plugin install evals@plurai-plugins
/reload-plugins
```

The plugin ships its own MCP server config, so Claude Code spawns the server automatically — no manual `.mcp.json` edits.

## Use it

```
/evals:eval I need to evaluate whether my RAG responses are grounded in the retrieved context
```

The plugin will:

1. **Create an evaluator** on the Plurai platform from your description.
2. **Ask refinement questions** through an interactive UI to clarify what "good" looks like.
3. **Optimize the evaluator** (LLM- or SLM-based, depending on the task).
4. **Return a deployed HTTPS endpoint** plus a per-evaluator API key you can call from your application.

> The evaluator key returned at step 4 is **separate** from the account key you used to sign in: the account key authenticates *you* to Plurai; the evaluator key authenticates *your app* to one specific deployed evaluator.

