#!/bin/bash
# Find python3 and run server.py — handles VS Code's minimal PATH.
# Forwards any args (e.g. `run.sh auth login`) through to server.py.
DIR="$(cd "$(dirname "$0")" && pwd)"
export PLUTO_API_BASE="${PLUTO_API_BASE:-https://pluto.stg.plurai.ai}"
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$p" ]; then
        exec "$p" "$DIR/src/server.py" "$@"
    fi
done
# Fallback to PATH
exec python3 "$DIR/src/server.py" "$@"
