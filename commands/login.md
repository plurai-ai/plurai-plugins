---
description: "Save your Plurai API key for the evals plugin"
allowed-tools: ["Bash"]
---

Save the user's Plurai API key so the evals MCP tools can authenticate.

1. Ask the user (in chat) to paste their Plurai API key. If they don't have one, tell them to:
   - Open https://app.plurai.ai/settings?tab=api-keys
   - Click **Create new key**
   - Copy the generated key and paste it here

   Warn that the key will appear in this conversation.
2. Run `uv run --project ${CLAUDE_PLUGIN_ROOT} python -m evals_mcp auth login --key <KEY>` with the key the user pasted.
3. On success the command prints `Saved API key to <path>.` — confirm to the user that they're set up and can now use `/eval`.
4. On failure it prints an error to stderr — relay it to the user.

Do not invoke any evals MCP tools (e.g. `evals_start_judge`) before the login command finishes successfully — they will fail with `Plurai API key not set.` until the key is saved.
