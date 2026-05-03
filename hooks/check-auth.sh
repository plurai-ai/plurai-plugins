#!/bin/bash
# SessionStart hook: detect missing evals credentials and tell Claude
# to prompt the user to run /login. Silent when already logged in.
CRED_PATH="${EVALS_CREDENTIALS_PATH:-$HOME/.config/evals/credentials.json}"
if [ ! -f "$CRED_PATH" ]; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "The evals plugin is installed but the user is NOT signed in (no credentials at ~/.config/evals/credentials.json). If the user invokes /eval or any evals_* MCP tool, do not call the tool — first tell them to run /login to save their Plurai API key, then proceed."
  }
}
EOF
fi
exit 0
