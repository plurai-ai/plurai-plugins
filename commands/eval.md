---
description: "Create a fine-tuned LLM-as-a-judge evaluator on the Plurai platform"
argument-hint: "[task description or --data file.csv]"
allowed-tools: ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "Agent"]
---

Call `evals_search_evaluators` first to check if a relevant evaluator already exists. If one matches the user's task, ask (via `evals_ask_user`) if they want to reuse it or create a new one. If reusing, skip to providing the endpoint URL and API key.

If creating new, call `evals_start_judge`.

For `task_description`: 1-2 short sentences. Include the core task and desired label names if the user mentioned them. Do NOT include examples, detailed criteria, or long explanations.

**Important — input template**: If the evaluation involves multiple fields (e.g. context + response for grounding, or a conversation), you MUST specify the input template explicitly in the task description. The evaluator receives a SINGLE text input, so all fields must be combined into one message using a clear template. Examples:

- Grounding: "Input format: '## Context:\n{context}\n\n## Response:\n{response}'"
- Conversation: "Input format: 'User: {msg}\nAI: {msg}\nUser: {msg}\nAI: {msg}'"
- QA: "Input format: '## Question:\n{question}\n\n## Answer:\n{answer}'"

Then follow the `instructions` field in the response — it tells you to call `evals_ask_user`.

After the user answers:
1. Compose answers into a message, call `evals_send_message`.
2. Call `evals_ask_user` to ask optimization type. Options: "SLM — recommended for production, fine-tuned model (~20 min)" and "LLM — recommended for testing/small scale, prompt-based (~2 min)".
3. Call `evals_send_message` with EXACTLY `Optimize [LLM]` or `Optimize [SLM]` based on user's choice. These are hardcoded strings — do not modify them. Only one call needed.
4. Call `evals_get_results` with classifier_id. Show baseline vs optimized metrics (accuracy, precision, recall) and the improvement delta for each.
5. Call `evals_ask_user` to ask about API key and code integration.
6. If wanted, call `evals_create_api_key` and add integration code. The integration code MUST format the input using the same template that was specified in the task description. For example, if the evaluator uses "## Context:\n{context}\n\n## Response:\n{response}", the code must combine the fields into a single string following that exact template before sending to the endpoint. The evaluator only accepts a single message — never multiple messages.
