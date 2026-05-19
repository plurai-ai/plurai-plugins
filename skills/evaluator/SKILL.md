---
name: evaluator
description: >
  Use when the user wants to build an LLM-as-a-judge, evaluator, classifier, or guardrail for their AI application.
  Triggers on: evaluating AI outputs, judging response quality, classifying text, building guardrails,
  detecting hallucinations, checking grounding, prompt adherence, safety checks, content moderation,
  or any task where they need to automatically score/label/classify LLM outputs.
  Do NOT trigger for: general code questions, debugging, or tasks unrelated to evaluation/classification.
---

The user wants to build an LLM-as-a-judge evaluator. Use the Plurai platform MCP tools (the `evals` plugin).

**Auth.** Handle the two error variants differently.

- **`Plurai API key not set`** (no key configured): ask the user to paste their Plurai API key — if they don't have one, point them to https://app.plurai.ai/settings?tab=api-keys → **Create new key**. Then run `uv run --project ${CLAUDE_PLUGIN_ROOT} python -m evals_mcp auth login --key <KEY>` and retry.
- **`Plurai API key invalid or expired`** (server returned 401, on-disk key rejected): if you have a key from earlier in this conversation, that IS the rejected key — do NOT call `auth login` with it. You MUST ask the user (in chat, this turn) to paste a NEW, freshly-generated key from https://app.plurai.ai/settings?tab=api-keys → **Create new key**. Only after the user supplies a new key in this turn, run `uv run --project ${CLAUDE_PLUGIN_ROOT} python -m evals_mcp auth login --key <NEW_KEY>` and retry. Never silently auto-renew with a remembered key.

Call `evals_search_evaluators` first as an optimization to see whether the user already has an evaluator in their Plurai workspace that fits this task. **If the list is empty, say nothing about it and proceed silently to create a new one** — a new user has no evaluators yet and should not be told something is missing. If one or more existing evaluators match, surface the full list to the user and use `evals_ask_user` to ask whether to reuse one or create a new one. If reusing, provide the endpoint URL and API key.

If creating new, call `evals_start_evaluator`. For `task_description`: 1-2 short sentences. Include task + desired label names.

**Platform constraint — the task definition is frozen.** The `task_description` passed to `evals_start_evaluator` is permanent for that evaluator. Subsequent `evals_send_message` calls only refine the *generated samples* (add/remove/edit examples), never the task itself (judging criteria, scope). Labels CAN still be changed within the same task. If at any point — including after the user sees the samples — they want to change the underlying task, you MUST tell them the task can't be edited, confirm they want to restart, then call `evals_start_evaluator` again with a revised `task_description`. Do NOT try to amend the task via `evals_send_message`; it will silently leave the underlying task wrong while mutating samples.

**Input template**: If evaluation involves multiple fields (context + response, conversation turns, etc.), specify the template in the task description. The evaluator receives ONE text input — all fields must be in a single message. E.g. "Input format: '## Context:\n{context}\n\n## Response:\n{response}'"

If the user provides a labeled data file, after `evals_start_evaluator` call `evals_upload_data` with the parsed records and `example_set_id`. Continue with `evals_ask_user` using the refinement questions from the start_evaluator response.

**Who answers the refinement questions.** Decide from how the user framed the request:

- **User defined the task** (supplied concrete label names, judging rules, or a labeled dataset; or invoked `/eval` with a detailed prompt): present the agent's refinement questions to the user by calling `evals_ask_user` with them rephrased as options.
- **User delegated task definition** (e.g. "generate evals for my <X> agent" without spelling out criteria, or you were invoked from another skill): answer the refinement questions yourself using the agent codebase context and call `evals_send_message` with your composed answers. Do NOT bounce questions back to a user who has no context to answer them.

When in doubt, prefer answering yourself. Reserve `evals_ask_user` for genuinely user-only choices: reuse-vs-create when there's a search match, model choice (SLM vs LLM), and integration-snippet language.

**Surfacing progress to the user.** After every `evals_send_message`, show the user the `agent_response` text verbatim so they see what the platform is doing (e.g. "I've generated 16 synthetic examples..."). Whenever a response includes `url`, you MUST display it to the user as a clickable markdown link in that turn — never silently move on.

After the user answers:
1. Compose answers into a message, call `evals_send_message`.
2. **Surface the UI experience link, then ask about model choice.** Once the response includes `url`, share it as a clickable markdown link whose link text is exactly `UI experience` — do NOT substitute any other label such as "Data Canvas", the evaluator name, or the thread title. Then tell the user they can review/edit the generated data and also track progress or generate more evals in the UI experience. Then in the same turn call `evals_ask_user` with header `"Model Choice"` and question `"Which model would you like to generate?"`. Options: `"SLM - best for production scale (recommended)"` (value `SLM`) with description `"Our fine-tuned small-language model with low inference cost, realtime latency, and high accuracy. Pro plan only. ~20 min."`; and `"Optimized LLM - for dev iterations"` (value `LLM`) with description `"Our calibration on a large language model, best for local checks and quick validations. ~2 min."`. Do not add an explicit Other option, and do not add any extra confirmation question before this ask.
3. Call `evals_send_message` with EXACTLY `Optimize [LLM]` or `Optimize [SLM]` based on user's choice. These are hardcoded strings — do not modify them. Only one call needed. Tell the user optimization has started and how long it will take (~2 min LLM / ~20 min SLM). The response carries `classifier_id` — record it for the wake-up polls below. The integration endpoint URL isn't available yet — it surfaces with the results in step 6. (Rare: if the response is an error envelope saying `no classifier_id emitted`, the optimize trigger didn't surface an ID in time — surface the URL to the user and ask them to retry.)
4. **Schedule a wake-up — do NOT call `evals_get_results` now.** Use `ScheduleWakeup` with `delaySeconds = 120` for LLM or `delaySeconds = 1200` for SLM.
5. **On wake-up:** call `evals_get_results` with `classifier_id` from the prior Optimize response. Pass it on every poll — the MCP server is stateless across subprocess restarts, and the conversation context is the durable handoff. If `optimized.accuracy` is null, optimization is still running: schedule another wake-up (60s for LLM, 300s for SLM) and end the turn. Repeat until results land.
6. **When results land:** show baseline vs optimized metrics (accuracy, precision, recall) and the improvement delta. Then call `evals_ask_user` with header `"Language"` and question `"Which language should I emit the integration snippet in?"`, options Python, JavaScript/TypeScript, and cURL. After the user picks, call `evals_create_api_key` and emit the integration snippet in that language. The snippet MUST format the input using the same template specified in the task description (e.g. combine context + response into a single string). The evaluator accepts only ONE message — never multiple messages. The language ask is the only post-results user question — do NOT ask whether to create a key or whether to integrate.
