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

If any `evals_*` tool fails with `Plurai API key not set.` or `Plurai API key invalid or expired.`, ask the user to run `/login` and stop until they do.

Call `evals_search_evaluators` first as an optimization to see whether the user already has an evaluator in their Plurai workspace that fits this task. **If the list is empty, say nothing about it and proceed silently to create a new one** — a new user has no evaluators yet and should not be told something is missing. If one or more existing evaluators match, surface the full list to the user and use `evals_ask_user` to ask whether to reuse one or create a new one. If reusing, provide the endpoint URL and API key.

If creating new, call `evals_start_evaluator`. For `task_description`: 1-2 short sentences. Include task + desired label names.

**Input template**: If evaluation involves multiple fields (context + response, conversation turns, etc.), specify the template in the task description. The evaluator receives ONE text input — all fields must be in a single message. E.g. "Input format: '## Context:\n{context}\n\n## Response:\n{response}'"

If the user provides a labeled data file, after `evals_start_evaluator` call `evals_upload_data` with the parsed records and `example_set_id`. Continue with `evals_ask_user` using the refinement questions from the start_evaluator response.

Then follow the `instructions` field in the response — it tells you to call `evals_ask_user`.

**Surfacing progress to the user.** After every `evals_send_message`, show the user the `agent_response` text verbatim so they see what the platform is doing (e.g. "I've generated 16 synthetic examples..."). Whenever a response includes `url`, you MUST display it to the user as a clickable markdown link in that turn — never silently move on.

After the user answers:
1. Compose answers into a message, call `evals_send_message`.
2. **Surface the UI experience link, then ask about model choice.** Once the response includes `url`, share it as a clickable markdown link describing it as the place to review/edit the generated data, and tell the user they can also track progress there. Then in the same turn call `evals_ask_user` with header `"Model Choice"` and question `"Which model would you like to generate?"`. Options: `"SLM - best for production scale (recommended)"` (value `SLM`) with description `"Our fine-tuned small-language model with low inference cost, realtime latency, and high accuracy. Pro plan only. ~20 min."`; and `"Optimized LLM - for dev iterations"` (value `LLM`) with description `"Our calibration on a large language model, best for local checks and quick validations. ~2 min."`. Do not add an explicit Other option, and do not add any extra confirmation question before this ask.
3. Call `evals_send_message` with EXACTLY `Optimize [LLM]` or `Optimize [SLM]` based on user's choice. These are hardcoded strings — do not modify them. Only one call needed. Tell the user optimization has started and how long it will take (~2 min LLM / ~20 min SLM). The integration endpoint URL isn't available yet — it surfaces with the results in step 6. (Rare: if the response is `optimization_started_pending_id` instead, the classifier hadn't been emitted yet when we polled — that's fine, the wake-up + `evals_get_results` flow below recovers automatically.)
4. **Schedule a wake-up — do NOT call `evals_get_results` now.** Use `ScheduleWakeup` with `delaySeconds = 120` for LLM or `delaySeconds = 1200` for SLM.
5. **On wake-up:** call `evals_get_results` (no arguments other than `response_format`). It reads the active classifier from session state — never substitute IDs from earlier responses. If the response is `classifier_pending`, optimization is still starting up: schedule another 60s wake-up and end the turn. If `optimized.accuracy` is null, optimization is still running: schedule another wake-up (60s for LLM, 300s for SLM) and end the turn. Repeat until results land.
6. **When results land:** show baseline vs optimized metrics (accuracy, precision, recall) and the improvement delta. Then call `evals_ask_user` with header `"Language"` and question `"Which language should I emit the integration snippet in?"`, options Python, JavaScript/TypeScript, and cURL. After the user picks, call `evals_create_api_key` and emit the integration snippet in that language. The snippet MUST format the input using the same template specified in the task description (e.g. combine context + response into a single string). The evaluator accepts only ONE message — never multiple messages. The language ask is the only post-results user question — do NOT ask whether to create a key or whether to integrate.
