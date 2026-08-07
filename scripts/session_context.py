#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Produce the session-start briefing: what this worktree is for, or what is free to pick up.

Reads the task graph from docs/tasks.yaml and the live issue state from `gh`. Written to degrade
rather than fail: without network or `gh`, it still reports the branch and the reading order.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

TASKS = Path("docs/tasks.yaml")
REPO = "taxpon/sentinel"
BRANCH_RE = re.compile(r"^task/(T\d+)-")


def sh(*args: str, timeout: int = 20) -> str | None:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def load_tasks() -> dict[str, dict]:
    data = yaml.safe_load(TASKS.read_text())
    return {t["id"]: t for t in data.get("tasks", [])}


def load_issues() -> dict[str, dict] | None:
    raw = sh(
        "gh", "issue", "list", "-R", REPO, "--label", "task", "--state", "all",
        "--limit", "200", "--json", "number,title,state,assignees",
    )
    if raw is None:
        return None
    by_task: dict[str, dict] = {}
    for issue in json.loads(raw):
        m = re.match(r"^(T\d+)\b", issue["title"])
        if m:
            by_task[m.group(1)] = issue
    return by_task


def bullet_list(items: list[str], limit: int = 8) -> list[str]:
    shown = [f"- {i}" for i in items[:limit]]
    if len(items) > limit:
        shown.append(f"- …and {len(items) - limit} more")
    return shown


def describe_current(task: dict, issue: dict | None) -> list[str]:
    lines = [f"## Your task: {task['id']} — {task['title']}", ""]
    if issue:
        lines.append(f"Issue: #{issue['number']} ({issue['state'].lower()})")
        lines.append("")
    if task.get("notes"):
        lines += [" ".join(str(task["notes"]).split()), ""]
    lines.append(f"**Spec:** `{task.get('spec', 'docs/index.md')}`")
    if task.get("adrs"):
        lines.append("**Decisions that constrain this task — read before starting:**")
        lines += [f"- `docs/adr/{slug}.md`" for slug in task["adrs"]]
    lines += ["", "**Owned files — do not modify anything outside this list:**"]
    lines += bullet_list([str(f) for f in task.get("owns", [])] or ["(none)"])
    if task.get("ui"):
        lines += [
            "",
            "This is a UI task: component tests are mandatory. Do not start a browser-level "
            "end-to-end test without asking on the issue first.",
        ]
    return lines


def describe_ready(tasks: dict[str, dict], issues: dict[str, dict] | None) -> list[str]:
    if issues is None:
        return [
            "## Task state unavailable",
            "",
            "Could not reach GitHub. List tasks manually:",
            "",
            "```bash",
            f"gh issue list -R {REPO} --label task --state open",
            "```",
        ]

    done = {tid for tid, i in issues.items() if i["state"].upper() == "CLOSED"}
    ready, blocked_count = [], 0
    for tid, task in sorted(tasks.items()):
        issue = issues.get(tid)
        if issue is None or issue["state"].upper() == "CLOSED":
            continue
        if issue["assignees"]:
            continue
        unmet = [d for d in task.get("depends_on", []) if d not in done]
        if unmet:
            blocked_count += 1
            continue
        ready.append((task["wave"], tid, task["title"], issue["number"]))

    if not ready:
        return [
            "## No unblocked tasks",
            "",
            f"{blocked_count} task(s) are waiting on dependencies; others are claimed or done.",
        ]

    ready.sort()
    lines = [
        f"## Ready to start ({len(ready)} unblocked, {blocked_count} waiting on dependencies)",
        "",
        "| Wave | Task | Issue |",
        "|---|---|---|",
    ]
    for wave, tid, title, number in ready[:12]:
        lines.append(f"| {wave} | {tid} — {title} | #{number} |")
    lines += [
        "",
        "Claim one, then work in its own worktree:",
        "",
        "```bash",
        f"gh issue edit <N> -R {REPO} --add-assignee @me --add-label status:claimed",
        "git worktree add ../wt/<Tid>-<slug> -b task/<Tid>-<slug>",
        "```",
    ]
    return lines


def main() -> int:
    if not TASKS.exists():
        return 0

    tasks = load_tasks()
    issues = load_issues()
    branch = (sh("git", "branch", "--show-current") or "").strip()

    out = ["# Sentinel — session briefing", ""]

    m = BRANCH_RE.match(branch)
    if m and m.group(1) in tasks:
        out += describe_current(tasks[m.group(1)], (issues or {}).get(m.group(1)))
    else:
        if branch:
            out += [f"Branch `{branch}` is not a task branch.", ""]
        out += describe_ready(tasks, issues)

    blockers = Path("docs/blockers.md")
    if blockers.exists():
        open_count = blockers.read_text().count("| Open ")
        if open_count:
            out += ["", f"**{open_count} open blockers** — see `docs/blockers.md` before assuming "
                        "an external dependency works."]

    out += [
        "",
        "---",
        "Rules are in `CLAUDE.md`. How to pick up work with no context: "
        "`docs/implementation-plan.md` (Cold start).",
    ]
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
