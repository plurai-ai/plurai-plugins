---
name: judge-evaluator
description: >
  Use when the user wants to build an LLM-as-a-judge, evaluator, classifier, or guardrail for their AI application.
  Triggers on: evaluating AI outputs, judging response quality, classifying text, building guardrails,
  detecting hallucinations, checking grounding, prompt adherence, safety checks, content moderation,
  or any task where they need to automatically score/label/classify LLM outputs.
  Do NOT trigger for: general code questions, debugging, or tasks unrelated to evaluation/classification.
---

The user wants to build an LLM-as-a-judge evaluator. Use the Pluto platform MCP tools.

If any pluto tool fails with `Pluto API key not set.` or `Pluto API key invalid or expired.`, ask the user to run `/pluto-judge:login` and stop until they do.

Call `pluto_search_evaluators` first to check if a relevant evaluator already exists. If one matches, ask (via `pluto_ask_user`) if they want to reuse it or create new. If reusing, provide the endpoint URL and API key.

If creating new, call `pluto_start_judge`. For `task_description`: 1-2 short sentences. Include task + desired label names.

**Input template**: If evaluation involves multiple fields (context + response, conversation turns, etc.), specify the template in the task description. The evaluator receives ONE text input — all fields must be in a single message. E.g. "Input format: '## Context:\n{context}\n\n## Response:\n{response}'"

Then follow the `instructions` field in the response — it tells you to call `pluto_ask_user`.

After the user answers:
1. Compose answers into a message, call `pluto_send_message`.
2. Call `pluto_ask_user` to ask optimization type. Options: "SLM — recommended for production, fine-tuned model (~20 min)" and "LLM — recommended for testing/small scale, prompt-based (~2 min)".
3. Call `pluto_send_message` with EXACTLY `Optimize [LLM]` or `Optimize [SLM]` based on user's choice. These are hardcoded strings — do not modify them. Only one call needed.
4. Call `pluto_get_results` with classifier_id. Show baseline vs optimized metrics (accuracy, precision, recall) and the improvement delta for each.
5. Call `pluto_ask_user` to ask about API key and code integration.
6. If wanted, call `pluto_create_api_key` and add integration code. The code MUST format the input using the same template specified in the task description (e.g. combine context + response into a single string). The evaluator accepts only ONE message — never multiple messages.
