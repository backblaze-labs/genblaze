#!/usr/bin/env bash
# PostToolUse hook: run `ruff format` on Python files Claude just edited.
# Matches the project's pre-commit config so formatting failures don't surface at PR time.
# Idempotent by design — skip if ruff is not on PATH.
set -e
command -v ruff >/dev/null 2>&1 || exit 0

path=$(python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("tool_input", {}).get("file_path", ""))
except Exception:
    pass
')

case "$path" in
  *.py) ruff format "$path" >/dev/null 2>&1 || true ;;
esac
exit 0
