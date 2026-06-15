# Evals — Claude Code Plugin

Create fine-tuned LLM-as-a-judge evaluators on the [Plurai platform](https://plurai.ai), directly from Claude Code. Describe what you want to evaluate, optionally answer a few refinement questions, and the plugin returns a deployed HTTPS endpoint you can call from your application.

## Requirements

- [Claude Code](https://docs.claude.com/claude-code)
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- A free [Plurai account](https://app.plurai.ai)

## 1. Get your API key

After creating your free [Plurai account](https://app.plurai.ai), go to https://app.plurai.ai/settings?tab=api-keys , create an API key, and keep it handy — you'll paste it into the console the first time you run the plugin. Your API key is stored locally on your machine (`~/.config/evals/credentials.json`) and sent only to Plurai's API to authenticate your requests.

## 2. Install

### Claude Code CLI

In any Claude Code session:

```
/plugin marketplace add plurai-ai/plurai-plugins
```

```
/plugin install evals@plurai-plugins
```

```
/reload-plugins
```

### IDE (VS Code / JetBrains)

1. Run `/plugins`.
2. In the **Marketplace** tab, add `plurai-ai/plurai-plugins`.
3. In the **Plugins** tab, install the **evals** plugin.
4. Hit **Restart**.

## 3. Use it

```
/evals:eval I need to evaluate whether my RAG responses are grounded in the retrieved context
```

The plugin will:

1. **Create an evaluator** on the Plurai platform from your description.
2. **Optionally ask refinement questions** through an interactive UI to clarify what "good" looks like — answer them to fine-tune the evaluator, or skip ahead.
3. **Optimize the evaluator** (LLM- or SLM-based, depending on the task).
4. **Return a deployed HTTPS endpoint** that you can call from your application using your existing Plurai API key — the same one you configured above. No second key is created.

## Troubleshooting

If an API key is missing, expired, or invalid, the plugin tells you so inline and links you to where to generate a new one — just follow the prompt. The items below cover problems the plugin can't report on its own.

### `/evals:eval` or the tools don't appear

The MCP server didn't start. Check that:

- You reloaded after installing — run `/reload-plugins`, or Restart in the IDE.
- [`uv`](https://docs.astral.sh/uv/) is installed and on your `PATH`, and you have **Python 3.11+**. The plugin needs `uv` to start its server; if it isn't found, the tools fail to load silently.

### Requests hang or fail with "Network error reaching Plurai"

If you're behind a corporate proxy or firewall, allowlist the hosts the plugin needs to reach: `app.plurai.ai`, `api.plurai.ai`, and `run.plurai.ai`.
