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

Call `pluto_start_judge` now. Do not output text first.

For `task_description`: 1-2 short sentences, max 150 characters. Include task + desired label names. Do NOT include examples or detailed criteria.

Then follow the `instructions` field in the response — it tells you to call `pluto_ask_user`.

After the user answers:
1. Compose answers into a message, call `pluto_send_message`.
2. Call `pluto_ask_user` to ask optimization type. Options: "SLM — recommended for production, fine-tuned model (~20 min)" and "LLM — recommended for testing/small scale, prompt-based (~2 min)".
3. Call `pluto_send_message` with EXACTLY `Optimize [LLM]` or `Optimize [SLM]` based on user's choice. These are hardcoded strings — do not modify them. Only one call needed.
4. Call `pluto_get_results` with classifier_id. Show baseline vs optimized metrics (accuracy, precision, recall) and the improvement delta for each.
5. Call `pluto_ask_user` to ask about API key and code integration.
6. If wanted, call `pluto_create_api_key` and add integration code.
