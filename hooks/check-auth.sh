#!/bin/bash
# SessionStart hook: detect missing pluto-judge credentials and tell Claude
# to prompt the user to run /pluto-judge:login. Silent when already logged in.
CRED_PATH="${PLUTO_CREDENTIALS_PATH:-$HOME/.config/pluto/credentials.json}"
if [ ! -f "$CRED_PATH" ]; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "pluto-judge is installed but the user is NOT signed in (no credentials at ~/.config/pluto/credentials.json). If the user invokes /judge or any pluto-judge MCP tool, do not call the tool — first tell them to run /pluto-judge:login to complete a one-time browser-based OAuth sign-in, then proceed."
  }
}
EOF
fi
exit 0
