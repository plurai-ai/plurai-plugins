#!/bin/bash
# Find python3 and run server.py — handles VS Code's minimal PATH
DIR="$(cd "$(dirname "$0")" && pwd)"
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$p" ]; then
        exec "$p" "$DIR/server.py"
    fi
done
# Fallback to PATH
exec python3 "$DIR/server.py"
