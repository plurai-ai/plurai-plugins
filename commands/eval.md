---
description: "Create a fine-tuned LLM-as-a-judge evaluator on the Plurai platform"
argument-hint: "[task description or --data file.csv]"
allowed-tools: ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "Agent"]
---

**Auth.** Two distinct cases — handle them differently.

**Case A — `Plurai API key not set`** (no key configured yet, e.g. fresh install or after `auth logout`):

1. Ask the user (in chat) to paste their Plurai API key. If they don't have one, point them to https://app.plurai.ai/settings?tab=api-keys → **Create new key**.
2. Run `uv run --project ${CLAUDE_PLUGIN_ROOT} python -m evals_mcp auth login --key <KEY>` with the pasted key.
3. On success (`Saved API key to <path>.`), retry the failed tool call. On failure, relay the stderr message to the user.

**Case B — `Plurai API key invalid or expired`** (server returned 401: the on-disk key was rejected):

The key currently on disk is bad. If you have a key from earlier in this conversation, that IS the rejected key — do NOT call `auth login` with it. You MUST ask the user (in chat, this turn) to paste a NEW, freshly-generated key from https://app.plurai.ai/settings?tab=api-keys → **Create new key**. Only after the user supplies a new key in this turn, run `uv run --project ${CLAUDE_PLUGIN_ROOT} python -m evals_mcp auth login --key <NEW_KEY>` and retry. Never silently auto-renew with a remembered key — the user must see the prompt and supply a fresh key.

**Case C — other errors (5xx, transport).** Any `{"error": ...}` envelope that's NOT an auth message will carry a `recovery_hint` field telling you exactly which tool to retry. Surface the error text to the user, ask whether to retry, and on yes call **the same tool that failed** with **the same arguments**. Do NOT escalate to `evals_start_evaluator` unless that's the tool that failed — restarting from `start_evaluator` creates a new thread and re-fires the entire flow. If the envelope includes a `thread_id`, that thread is still alive on the platform and the retry will resume it.

Call `evals_search_evaluators` first as an optimization to check whether the user already has an evaluator in their Plurai workspace that fits this task. **If the list is empty, say nothing about it and proceed silently to create a new one** — a fresh user has no evaluators yet and should not be told something is missing. If one or more existing evaluators match, surface the full list to the user and use `evals_ask_user` to ask whether to reuse one or create a new one. If reusing, skip to providing the endpoint URL and API key.

If creating new, call `evals_start_evaluator`.

For `task_description`: 1-2 short sentences. Include the core task and desired label names if the user mentioned them. Do NOT include examples, detailed criteria, or long explanations.

**Platform constraint — the task definition is frozen.** The `task_description` passed to `evals_start_evaluator` is permanent for that evaluator. Subsequent `evals_send_message` calls only refine the *generated samples* (add/remove/edit examples), never the task itself (judging criteria, scope). Labels CAN still be changed within the same task. If at any point — including after the user sees the samples — they want to change the underlying task, you MUST tell them the task can't be edited, confirm they want to restart, then call `evals_start_evaluator` again with a revised `task_description`. Do NOT try to amend the task via `evals_send_message`; it will silently leave the underlying task wrong while mutating samples.

**Important — input template**: If the evaluation involves multiple fields (e.g. context + response for grounding, or a conversation), you MUST specify the input template explicitly in the task description. The evaluator receives a SINGLE text input, so all fields must be combined into one message using a clear template. Examples:

- Grounding: "Input format: '## Context:\n{context}\n\n## Response:\n{response}'"
- Conversation: "Input format: 'User: {msg}\nAI: {msg}\nUser: {msg}\nAI: {msg}'"
- QA: "Input format: '## Question:\n{question}\n\n## Answer:\n{answer}'"

**Data path — if the user supplied a labeled data file** (e.g. `/eval --data path/to/file.csv` or they pasted a path): after `evals_start_evaluator`, read the file, parse it into `{sample, label, reasoning?}` records, then call `evals_upload_data` with the `example_set_id` from the start_evaluator response. Do NOT synthesize records. Continue with `evals_ask_user` using the refinement questions from the start_evaluator `agent_response`.

**Delegate or ask — applied per decision.**

At every decision point in the flow (refinement questions, model choice, integration language), the rule is the same: if the user's intent already speaks to *this* decision, act on it; otherwise ask.

- **Refinement questions** cover the OVERALL task (labels, scope, judging criteria) — never per-example. If the user handed you a spec source for them, answer yourself via `evals_send_message` from that source; otherwise route them to the user via `evals_ask_user`, rephrased as options.
- **Other decisions** (model choice, integration language) are independent. Delegation on one does not imply delegation on another — e.g. a user who pointed at a codebase but said nothing about model still gets the SLM/LLM ask.

When ambiguous, ask. User-facing questions ALWAYS go through `evals_ask_user` — never invent a plain-text Q&A turn.

**Surfacing progress to the user.** After every `evals_send_message`, show the user the `agent_response` text verbatim so they see what the platform is doing (e.g. "I've generated 16 synthetic examples..."). Whenever a response includes `url`, you MUST display it to the user as a clickable markdown link in that turn — never silently move on.

After the refinement round (whether the user answered via `evals_ask_user` or you self-answered via `evals_send_message`):
1. If the user answered, compose their answers and call `evals_send_message`. If you self-answered, you've already done this — proceed.
2. **Surface the UI experience link, then settle the model.** Once the response includes `url`, share it as a clickable markdown link whose link text is exactly `UI experience` — do NOT substitute any other label such as "Data Canvas", the evaluator name, or the thread title. Then tell the user they can review/edit the generated data and track progress in the UI experience.

   Apply the per-decision rule: if the user already specified a model preference upfront, use it (jump to step 3 with `Optimize [LLM]` or `Optimize [SLM]`). Otherwise, call `evals_ask_user` with header `"Model Choice"` and question `"Which model would you like to generate?"`. Default options:
   - label `"SLM - best for production scale (recommended)"`, description `"Our fine-tuned small-language model with low inference cost, realtime latency, and high accuracy. Pro plan only. ~20 min."`
   - label `"Optimized LLM - for dev iterations"`, description `"Our calibration on a large language model, best for local checks and quick validations. ~2 min."`

   **Gate on `slm_allowed` (from the response).** If `false`, swap the option list (do NOT drop to one option — `evals_ask_user` rejects single-option questions). Use these two options, and prepend the upgrade prompt verbatim to the question: "SLM optimization requires a paid Plurai plan. Upgrade at [Plurai Settings](https://app.plurai.ai/settings?tab=subscription-billing) to unlock the fine-tuned small-language model."
   - label `"Continue with Optimized LLM"`, description `"Our calibration on a large language model, best for local checks and quick validations. ~2 min."`
   - label `"Wait — I'll upgrade my plan first"`, description `"Stop here for now. After upgrading at Plurai Settings, resume this flow and SLM will be available."`

   If the user picks "Wait", do NOT call `evals_send_message` — acknowledge and end the turn. Otherwise proceed to step 3 with `Optimize [LLM]`.

   Do not add an explicit Other option, and do not add any extra confirmation question before this ask.
3. Call `evals_send_message` with EXACTLY `Optimize [LLM]` or `Optimize [SLM]` based on user's choice. These are hardcoded strings — do not modify them. Only one call needed. Tell the user optimization has started and how long it will take (~2 min LLM / ~20 min SLM). The response carries `classifier_id` — record it for the wake-up polls below. The integration endpoint URL isn't available yet — it surfaces with the results in step 6. **If the response is the "still running in the background" envelope** (common under batch load), the optimize is running and the server enforces ONE optimize run per thread — schedule a `ScheduleWakeup(delaySeconds=120)` and call `evals_send_message(thread_id, '<same Optimize message>')` again on wake. The server is idempotent per thread: that resume re-awaits the existing run, it never restarts it. Never resend the message until the wake-up fires, and never change the message.
4. **Schedule a wake-up — do NOT call `evals_get_results` now.** Use `ScheduleWakeup` with `delaySeconds = 120` for LLM or `delaySeconds = 1200` for SLM.
5. **On wake-up:** call `evals_get_results` with `classifier_id` from the prior Optimize response. Pass it on every poll — the MCP server is stateless across subprocess restarts, and the conversation context is the durable handoff. If `optimized.accuracy` is null, optimization is still running: schedule another wake-up (60s for LLM, 300s for SLM) and end the turn. Repeat until results land.
6. **When results land:** show baseline vs optimized metrics (accuracy, precision, recall) and the improvement delta. Then call `evals_ask_user` with header `"Language"` and question `"Which language should I emit the integration snippet in?"`, options Python, JavaScript/TypeScript, and cURL. After the user picks, call `evals_create_api_key` and emit the integration snippet in that language. The integration code MUST format the input using the same template specified in the task description. For example, if the evaluator uses "## Context:\n{context}\n\n## Response:\n{response}", the code must combine the fields into a single string following that exact template before sending to the endpoint. The evaluator only accepts a single message — never multiple messages. The language ask is the only post-results user question — do NOT ask whether to create a key or whether to integrate.
