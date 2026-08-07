#!/usr/bin/env bash
# SessionStart hook: inject live task state into the session context.
#
# A static CLAUDE.md cannot answer "what is free to work on right now" — that
# depends on which pull requests are merged and which issues are assigned. This
# hook computes it from docs/tasks.yaml and the GitHub issues.
#
# Failing softly is deliberate: a broken hook must never prevent a session from
# starting, so any error path emits empty context and exits 0.

set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || echo .)}" || exit 0

emit_empty() { exit 0; }

command -v uv >/dev/null 2>&1 || emit_empty
[ -f scripts/session_context.py ] || emit_empty

context="$(uv run --quiet scripts/session_context.py 2>/dev/null)" || emit_empty
[ -n "$context" ] || emit_empty

python3 -c '
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": sys.stdin.read(),
    }
}))' <<<"$context"
