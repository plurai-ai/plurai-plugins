---
description: "Create a fine-tuned LLM-as-a-judge evaluator on the Plurai platform"
argument-hint: "[task description or --data file.csv]"
allowed-tools: ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "Agent"]
---

**Auth.** Two distinct cases — handle them differently.

**Case A — `Plurai API key not set`** (no key configured yet, e.g. fresh install or after `auth logout`):

1. Ask the user (in chat) to paste their Plurai API key. If they don't have one, point them to https://app.plurai.ai/settings?tab=api-keys → **Create new key**. Warn that the key will appear in this conversation.
2. Run `uv run --project ${CLAUDE_PLUGIN_ROOT} python -m evals_mcp auth login --key <KEY>` with the pasted key.
3. On success (`Saved API key to <path>.`), retry the failed tool call. On failure, relay the stderr message to the user.

**Case B — `Plurai API key invalid or expired`** (server returned 401: the on-disk key was rejected):

The key currently on disk is bad. If you have a key from earlier in this conversation, that IS the rejected key — do NOT call `auth login` with it. You MUST ask the user (in chat, this turn) to paste a NEW, freshly-generated key from https://app.plurai.ai/settings?tab=api-keys → **Create new key**. Warn the key will appear in this conversation. Only after the user supplies a new key in this turn, run `uv run --project ${CLAUDE_PLUGIN_ROOT} python -m evals_mcp auth login --key <NEW_KEY>` and retry. Never silently auto-renew with a remembered key — the user must see the prompt and supply a fresh key.

Call `evals_search_evaluators` first as an optimization to check whether the user already has an evaluator in their Plurai workspace that fits this task. **If the list is empty, say nothing about it and proceed silently to create a new one** — a fresh user has no evaluators yet and should not be told something is missing. If one or more existing evaluators match, surface the full list to the user and use `evals_ask_user` to ask whether to reuse one or create a new one. If reusing, skip to providing the endpoint URL and API key.

If creating new, call `evals_start_evaluator`.

For `task_description`: 1-2 short sentences. Include the core task and desired label names if the user mentioned them. Do NOT include examples, detailed criteria, or long explanations.

**Important — input template**: If the evaluation involves multiple fields (e.g. context + response for grounding, or a conversation), you MUST specify the input template explicitly in the task description. The evaluator receives a SINGLE text input, so all fields must be combined into one message using a clear template. Examples:

- Grounding: "Input format: '## Context:\n{context}\n\n## Response:\n{response}'"
- Conversation: "Input format: 'User: {msg}\nAI: {msg}\nUser: {msg}\nAI: {msg}'"
- QA: "Input format: '## Question:\n{question}\n\n## Answer:\n{answer}'"

**Data path — if the user supplied a labeled data file** (e.g. `/eval --data path/to/file.csv` or they pasted a path): after `evals_start_evaluator`, read the file, parse it into `{sample, label, reasoning?}` records, then call `evals_upload_data` with the `example_set_id` from the start_evaluator response. Do NOT synthesize records. Continue with `evals_ask_user` using the refinement questions from the start_evaluator `agent_response`.

Then follow the `instructions` field in the response — it tells you to call `evals_ask_user`.

**Surfacing progress to the user.** After every `evals_send_message`, show the user the `agent_response` text verbatim so they see what the platform is doing (e.g. "I've generated 16 synthetic examples..."). Whenever a response includes `url`, you MUST display it to the user as a clickable markdown link in that turn — never silently move on.

After the user answers:
1. Compose answers into a message, call `evals_send_message`.
2. **Surface the UI experience link, then ask about model choice.** Once the response includes `url`, share it as a clickable markdown link whose link text is exactly `UI experience` — do NOT substitute any other label such as "Data Canvas", the evaluator name, or the thread title. Then tell the user they can review/edit the generated data and also track progress or generate more evals in the UI experience. Then in the same turn call `evals_ask_user` with header `"Model Choice"` and question `"Which model would you like to generate?"`. Options: `"SLM - best for production scale (recommended)"` (value `SLM`) with description `"Our fine-tuned small-language model with low inference cost, realtime latency, and high accuracy. Pro plan only. ~20 min."`; and `"Optimized LLM - for dev iterations"` (value `LLM`) with description `"Our calibration on a large language model, best for local checks and quick validations. ~2 min."`. Do not add an explicit Other option, and do not add any extra confirmation question before this ask.
3. Call `evals_send_message` with EXACTLY `Optimize [LLM]` or `Optimize [SLM]` based on user's choice. These are hardcoded strings — do not modify them. Only one call needed. Tell the user optimization has started and how long it will take (~2 min LLM / ~20 min SLM). The response carries `classifier_id` — record it for the wake-up polls below. The integration endpoint URL isn't available yet — it surfaces with the results in step 6. (Rare: if the response is an error envelope saying `no classifier_id emitted`, the optimize trigger didn't surface an ID in time — surface the URL to the user and ask them to retry.)
4. **Schedule a wake-up — do NOT call `evals_get_results` now.** Use `ScheduleWakeup` with `delaySeconds = 120` for LLM or `delaySeconds = 1200` for SLM.
5. **On wake-up:** call `evals_get_results` with `classifier_id` from the prior Optimize response. Pass it on every poll — the MCP server is stateless across subprocess restarts, and the conversation context is the durable handoff. If `optimized.accuracy` is null, optimization is still running: schedule another wake-up (60s for LLM, 300s for SLM) and end the turn. Repeat until results land.
6. **When results land:** show baseline vs optimized metrics (accuracy, precision, recall) and the improvement delta. Then call `evals_ask_user` with header `"Language"` and question `"Which language should I emit the integration snippet in?"`, options Python, JavaScript/TypeScript, and cURL. After the user picks, call `evals_create_api_key` and emit the integration snippet in that language. The integration code MUST format the input using the same template specified in the task description. For example, if the evaluator uses "## Context:\n{context}\n\n## Response:\n{response}", the code must combine the fields into a single string following that exact template before sending to the endpoint. The evaluator only accepts a single message — never multiple messages. The language ask is the only post-results user question — do NOT ask whether to create a key or whether to integrate.
