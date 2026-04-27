---
description: "Sign in to pluto-judge via browser-based OAuth"
allowed-tools: ["Bash"]
---

Run `${CLAUDE_PLUGIN_ROOT}/run.sh auth login` to start the browser-based OAuth login flow for pluto-judge.

The command opens the user's default browser to the Pluto sign-in page and waits up to 5 minutes for them to complete sign-in. When it finishes:
- On success it prints `Logged in as <email>.` — confirm to the user that they're signed in and can now use `/judge`.
- On failure it prints an error to stderr — relay it to the user.

Do not invoke any pluto-judge MCP tools (e.g. `pluto_start_judge`) before the login command finishes successfully.
