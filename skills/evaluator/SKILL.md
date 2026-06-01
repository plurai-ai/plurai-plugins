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

**Other errors (5xx, transport).** Any `{"error": ...}` envelope that's NOT an auth message will carry a `recovery_hint` field telling you exactly which tool to retry. Surface the error text to the user, ask whether to retry, and on yes call **the same tool that failed** with **the same arguments**. Do NOT escalate to `evals_start_evaluator` unless that's the tool that failed — restarting from `start_evaluator` creates a new thread and re-fires the entire flow. If the envelope includes a `thread_id`, that thread is still alive on the platform and the retry will resume it.

Call `evals_search_evaluators` first as an optimization to see whether the user already has an evaluator in their Plurai workspace that fits this task. **If the list is empty, say nothing about it and proceed silently to create a new one** — a new user has no evaluators yet and should not be told something is missing. If one or more existing evaluators match, surface the full list to the user and use `evals_ask_user` to ask whether to reuse one or create a new one. If reusing, jump straight to step 6 (integration snippet): the endpoint URL is the `endpoint_url` from the search result for the chosen evaluator, and the API key comes from `evals_get_api_key` (the same Plurai API key the user configured at session start — no new key is created).

If creating new, call `evals_start_evaluator`. For `task_description`: 1-2 short sentences describing the core task. Include desired label names **only if the user already mentioned them** — do NOT pre-ask the user for labels, scope, or criteria before this call. The agent's refinement round covers all of those, and pre-asking causes the user to see the labels question twice.

**Platform constraint — the task definition is frozen.** The `task_description` passed to `evals_start_evaluator` is permanent for that evaluator. Subsequent `evals_send_message` calls only refine the *generated samples* (add/remove/edit examples), never the task itself (judging criteria, scope). Labels CAN still be changed within the same task. If at any point — including after the user sees the samples — they want to change the underlying task, you MUST tell them the task can't be edited, confirm they want to restart, then call `evals_start_evaluator` again with a revised `task_description`. Do NOT try to amend the task via `evals_send_message`; it will silently leave the underlying task wrong while mutating samples.

**Input template**: If evaluation involves multiple fields (context + response, conversation turns, etc.), specify the template in the task description. The evaluator receives ONE text input — all fields must be in a single message. E.g. "Input format: '## Context:\n{context}\n\n## Response:\n{response}'"

If the user provides a labeled data file, after `evals_start_evaluator` call `evals_upload_data` with the parsed records and `example_set_id`. Continue with `evals_ask_user` using the refinement questions from the start_evaluator response.

**Delegate or ask — applied per decision.**

At every decision point in the flow (refinement questions, model choice, integration language), the rule is the same: if the user's intent already speaks to *this* decision, act on it; otherwise ask.

- **Refinement questions** cover the OVERALL task (labels, scope, judging criteria) — never per-example. If the user handed you a spec source for them, answer yourself via `evals_send_message` from that source; otherwise route them to the user via `evals_ask_user`, rephrased as options.
- **Other decisions** (model choice, integration language) are independent. Delegation on one does not imply delegation on another — e.g. a user who pointed at a codebase but said nothing about model still gets the SLM/LLM ask.

When ambiguous, ask. User-facing questions ALWAYS go through `evals_ask_user` — never invent a plain-text Q&A turn.

**Recommend a default when one option is clearly best.** Whenever you call `evals_ask_user` and one of the options is the defensible default (the one most users in this situation should pick), append `(Recommended)` to that option's label and list it first. Apply this to refinement questions (labels, scope, judging criteria), the reuse-vs-create-new ask, and the Language ask — anywhere a defensible default exists. If the options are genuinely equivalent for this user (no clear winner), do not force one — list them in a natural order without a marker.

**Skip handling (refinement only).** Treat the host's `AskUserQuestion` as a *partial* form: the user may submit having answered only some tabs, or escape/decline the whole thing.
- For any refinement question that **has a user answer in the response**: use that answer when composing `evals_send_message`.
- For any refinement question with **no user answer** (omitted from `answers`, or the whole response is `"User declined to answer questions"`/interrupted): fill it yourself with the orchestrator's own pick using sensible defaults.

Then send a single `evals_send_message` composing the merged answers and continue the normal post-refinement flow. Do NOT retry the ask, do NOT re-prompt the user for the missing questions, do NOT surface the interruption text verbatim, do NOT stall.

**Surfacing progress to the user.** After every `evals_send_message`, show the user the `agent_response` text verbatim so they see what the platform is doing (e.g. "I've generated 16 synthetic examples..."). Whenever a response includes `url`, you MUST display it to the user as a clickable markdown link in that turn — never silently move on.

After the refinement round (whether the user answered via `evals_ask_user` or you self-answered via `evals_send_message`):
1. If the user answered, compose their answers and call `evals_send_message`. If you self-answered, you've already done this — proceed.
2. **Surface the UI experience link, then settle the model.** Once the response includes `url`, share it as a clickable markdown link whose link text is exactly `UI experience` — do NOT substitute any other label such as "Data Canvas", the evaluator name, or the thread title. Then tell the user they can review/edit the generated data and track progress in the UI experience.

   Apply the per-decision rule: if the user already specified a model preference upfront, use it (jump to step 3 with `Optimize [LLM]` or `Optimize [SLM]`). Otherwise, call `evals_ask_user` with header `"Model Choice"` and question `"Which model would you like to generate?"`. Default options:
   - label `"SLM - best for production scale (Recommended)"`, description `"Our fine-tuned small-language model with low inference cost, realtime latency, and high accuracy. Pro plan only. ~20 min."`
   - label `"Optimized LLM - for dev iterations"`, description `"Our calibration on a large language model, best for local checks and quick validations. ~2 min."`

   **Whenever the SLM option is presented to the user (either as the recommended choice above or in the upgrade-gated variant below), you MUST first emit a one-line plain-text message containing the clickable markdown link `[Learn more about intent-calibrated SLMs](https://intercom.help/plurai/en/articles/14113048-intent-calibrated-slms-to-the-rescue)` BEFORE calling `evals_ask_user`.** Do NOT embed the URL inside the `question` or any option `description` — those fields render as plain text in the picker and the link will not be clickable. Keep it in the conversational text that precedes the ask.

   **Gate on `slm_allowed` (from the response).** If `false`, swap the option list (do NOT drop to one option — `evals_ask_user` rejects single-option questions). Use these two options, and prepend the upgrade prompt verbatim to the question: "SLM optimization requires a paid Plurai plan. Upgrade at https://app.plurai.ai/settings?tab=subscription-billing to unlock the fine-tuned small-language model." (Plain URL inside the question — render the clickable `[Plurai Settings](...)` and `[Learn more about intent-calibrated SLMs](...)` markdown links in the text turn that precedes the ask.)
   - label `"Continue with Optimized LLM"`, description `"Our calibration on a large language model, best for local checks and quick validations. ~2 min."`
   - label `"Wait — I'll upgrade my plan first"`, description `"Stop here for now. After upgrading at Plurai Settings, resume this flow and SLM will be available."`

   If the user picks "Wait", do NOT call `evals_send_message` — acknowledge and end the turn. Otherwise proceed to step 3 with `Optimize [LLM]`.

   Do not add an explicit Other option, and do not add any extra confirmation question before this ask.

   **On Other / decline / ambiguous answer.** Don't fire optimization without an explicit pick — neither `Optimize [LLM]` nor `Optimize [SLM]`. If free text clearly maps to one of them, use that. On confusion, answer inline and re-ask once. Otherwise restate the two options in plain text and end the turn.
3. Call `evals_send_message` with EXACTLY `Optimize [LLM]` or `Optimize [SLM]` based on user's choice. These are hardcoded strings — do not modify them. Only one call needed. Tell the user optimization has started and how long it will take (~2 min LLM / ~20 min SLM). The response carries `classifier_id` — record it for the wake-up polls below. The integration endpoint URL isn't available yet — it surfaces with the results in step 6. **If the response is the "still running in the background" envelope** (common under batch load), the optimize is running and the server enforces ONE optimize run per thread — schedule a `ScheduleWakeup(delaySeconds=120)` and call `evals_send_message(thread_id, '<same Optimize message>')` again on wake. The server is idempotent per thread: that resume re-awaits the existing run, it never restarts it. Never resend the message until the wake-up fires, and never change the message.
4. **Schedule a wake-up and end the turn.** Use `ScheduleWakeup` with `delaySeconds = 120` for LLM or `delaySeconds = 1200` for SLM. The MCP responses carry the wait/poll contract themselves; follow the `instructions` field they return.
5. **On wake-up:** call `evals_get_results(classifier_id)`. Pass `classifier_id` every time — the MCP server is stateless across subprocess restarts. If still pending, the response's `instructions` field tells you the re-poll interval (60s LLM, 300s SLM); schedule another wake-up and end the turn. Repeat until results land.
6. **When results land:** show baseline vs optimized metrics (accuracy, precision, recall) and the improvement delta. Then call `evals_ask_user` with header `"Language"` and question `"Which language should I emit the integration snippet in?"`, options Python, JavaScript/TypeScript, and cURL. After the user picks, call `evals_get_api_key` to retrieve the user's existing Plurai API key (the one they configured at session start — this is a local read, **not** a new key creation) and embed it in the integration snippet. The snippet MUST format the input using the same template specified in the task description (e.g. combine context + response into a single string). The evaluator accepts only ONE message — never multiple messages. The language ask is the only post-results user question — do NOT ask whether to create a key or whether to integrate.
