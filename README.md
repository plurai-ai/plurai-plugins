# Evals & Guardrails SLMs — Claude Code Plugin

Turn a simple task description (or a few examples) into a deployed SLM for online **evals** or **guardrails**, directly from Claude Code. You write a description; it handles data generation, labeling, fine-tuning, and serving, returning a live HTTPS endpoint in minutes.

The resulting SLM runs in real time at sub-100ms, with up to 93% lower latency, 43% lower failure rate, and 87% cost savings versus frontier LLM judges. Backed by our ICML 2026 research paper, [BARRED](https://arxiv.org/abs/2604.25203).

## Requirements

- Claude Code  
- Python 3.11+ with [`uv`](https://docs.astral.sh/uv/) on your `PATH`  
- A free [Plurai account](https://app.plurai.ai/claude?step=guide)

## Quickstart

**1\. Get your API key.** Create a free [Plurai account](https://app.plurai.ai/claude?step=guide), generate a key, and paste it into the Claude console on first run. Your key is stored locally (`~/.config/evals/credentials.json`) and used only to authenticate with Plurai's API.
**2\. Install** — run these one at a time in any Claude Code session:

```
/plugin marketplace add plurai-ai/plurai-plugins
```

```
/plugin install evals@plurai-plugins
```

```
/reload-plugins
```

VS Code / JetBrains: run `/plugins`, add `plurai-ai/plurai-plugins` in the Marketplace tab, install the evals plugin, then Restart.

**3\. Run it.**

```
/evals:eval Evaluate whether my RAG responses are grounded in the retrieved context
```

The plugin optionally asks refinement questions to sharpen what "good" looks like, fine-tunes an SLM-based eval or guardrail tailored to your use case, and returns an endpoint you call with the same API key.

## Troubleshooting

- **API key invalid or missing** — the plugin links you to generate a new one; paste it into the console.  
- **`/evals:eval` doesn't appear** — the MCP server didn't start. Reload (`/reload-plugins` or Restart), and confirm `uv` is on your `PATH` with Python 3.11+. Without `uv`, the tools fail to load silently.  
- **Requests hang / "Network error reaching Plurai"** — allowlist `app.plurai.ai`, `api.plurai.ai`, `run.plurai.ai`.
