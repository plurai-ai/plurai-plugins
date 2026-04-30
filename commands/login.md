---
description: "Save your Pluto API key for pluto-judge"
allowed-tools: ["Bash"]
---

Save the user's Pluto API key so the pluto-judge MCP tools can authenticate.

1. Ask the user (in chat) to paste their Pluto API key. Tell them they can get one from https://pluto.plurai.ai. Warn that the key will appear in this conversation.
2. Run `uv run --project ${CLAUDE_PLUGIN_ROOT} python -m pluto_judge auth login --key <KEY>` with the key the user pasted.
3. On success the command prints `Saved API key to <path>.` — confirm to the user that they're set up and can now use `/pluto-judge:judge`.
4. On failure it prints an error to stderr — relay it to the user.

Do not invoke any pluto-judge MCP tools (e.g. `pluto_start_judge`) before the login command finishes successfully — they will fail with `Pluto API key not set.` until the key is saved.
