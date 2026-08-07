#!/usr/bin/env bash
# PreToolUse hook: refuse to create a pull request that has not been reviewed.
#
# CLAUDE.md states the rule, but the docs are explicit that CLAUDE.md is context
# rather than enforcement. This is the enforcement.
#
# `/finish-task` runs pr-review-toolkit:review-pr and, once there are no findings
# left, writes the reviewed commit SHA to .sentinel-review/<branch>.ok. This hook
# lets `gh pr create` through only while that SHA still matches HEAD, so pushing
# further commits invalidates the approval automatically.
#
# A human can bypass deliberately by writing the marker themselves — visible in
# the working tree rather than silent.

set -uo pipefail

payload="$(cat)"

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || echo .)}" || exit 0

read -r -d '' PY <<'PYEOF'
import json, subprocess, sys
from pathlib import Path

payload = json.load(sys.stdin)
command = (payload.get("tool_input") or {}).get("command", "")

# Match `gh pr create` even when it is buried in a pipeline or has flags before it.
tokens = command.split()
is_pr_create = any(
    tokens[i] == "gh" and "pr" in tokens[i + 1 : i + 4] and "create" in tokens[i + 1 : i + 5]
    for i, _ in enumerate(tokens)
)
if not is_pr_create:
    sys.exit(0)


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True
    ).stdout.strip()


branch = git("branch", "--show-current")
head = git("rev-parse", "HEAD")
if not branch or not head:
    deny("Not on a branch with commits; cannot verify the pre-PR review.")

marker = Path(".sentinel-review") / f"{branch.replace('/', '_')}.ok"

if not marker.exists():
    deny(
        f"Pre-PR review has not been run for `{branch}`.\n\n"
        "Run `/finish-task` — it applies pr-review-toolkit:review-pr and records the result "
        "once no findings remain. See rule 3 in CLAUDE.md.\n\n"
        "If pr-review-toolkit is not installed on this machine, install it or ask the human "
        "to record an explicit bypass; do not skip the review silently."
    )

reviewed = marker.read_text().strip()
if reviewed != head:
    deny(
        f"The recorded review is stale: it covers {reviewed[:12]}, but HEAD is {head[:12]}.\n\n"
        "Commits were added after the review. Run `/finish-task` again so the review covers "
        "the code that will actually be in the pull request."
    )
PYEOF

printf '%s' "$payload" | python3 -c "$PY"
