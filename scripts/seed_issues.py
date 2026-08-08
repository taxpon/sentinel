#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Create and update the GitHub task issues from docs/tasks.yaml.

Two passes are needed: dependency references have to be written as `#<number>`, and those numbers
only exist once the issues do. Pass one creates anything missing, pass two rewrites every body with
the real numbers resolved.

Idempotent — existing issues are matched by their `T<id>:` title prefix and updated in place, so
this can be re-run after editing tasks.yaml.

    uv run scripts/seed_issues.py --dry-run
    uv run scripts/seed_issues.py
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

TASKS = Path("docs/tasks.yaml")
REPO = "taxpon/sentinel"

DOD = """## Definition of Done

- [ ] Implemented as specified
- [ ] Unit tests added; `make test` is green
- [ ] `ruff` and `mypy` pass
- [ ] No files changed outside "Owned files"
- [ ] `/finish-task` run: review applied, no findings remaining
- [ ] Divergence from the spec fixed in `docs/`
- [ ] Non-obvious decisions recorded in `docs/adr/`; `make adr-index` re-run"""

UI_DOD = """- [ ] Component tests cover rendering, prop branches, empty and error states, and formatting
- [ ] No browser-level test was written — they are out of scope; any gap a component test could not
      reach is named in the pull request"""


def gh(*args: str, check: bool = True) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def toposort(tasks: dict[str, dict]) -> list[str]:
    """Order tasks so dependencies come first; raises on a cycle or a dangling reference."""
    ordered: list[str] = []
    temporary: set[str] = set()

    def visit(tid: str, trail: list[str]) -> None:
        if tid in ordered:
            return
        if tid in temporary:
            raise SystemExit(f"Dependency cycle: {' -> '.join([*trail, tid])}")
        temporary.add(tid)
        for dep in tasks[tid].get("depends_on", []):
            if dep not in tasks:
                raise SystemExit(f"{tid} depends on unknown task {dep}")
            visit(dep, [*trail, tid])
        temporary.discard(tid)
        ordered.append(tid)

    for tid in sorted(tasks):
        visit(tid, [])
    return ordered


def render_body(task: dict, numbers: dict[str, int]) -> str:
    parts = [f"## Goal\n\n{' '.join(str(task.get('notes', task['title'])).split())}"]

    parts.append(f"## Spec\n\n[`{task['spec']}`](../blob/main/{task['spec'].split('#')[0]})")

    owns = task.get("owns") or []
    owned = "\n".join(f"- `{f}`" for f in owns) if owns else "_None — this task produces no files._"
    parts.append(
        "## Owned files\n\n"
        "Do not modify anything outside this list; it is how parallel sessions avoid collisions.\n\n"
        f"{owned}"
    )

    deps = task.get("depends_on") or []
    if deps:
        lines = []
        for dep in deps:
            ref = f"#{numbers[dep]}" if dep in numbers else dep
            lines.append(f"- {ref} — {dep}")
        parts.append(
            "## Depends on\n\nAll of these must be **merged** before starting.\n\n" + "\n".join(lines)
        )

    adrs = task.get("adrs") or []
    if adrs:
        links = "\n".join(f"- [`{a}`](../blob/main/docs/adr/{a}.md)" for a in adrs)
        parts.append(f"## Related decisions\n\nRead before starting.\n\n{links}")

    dod = DOD + ("\n" + UI_DOD if task.get("ui") else "")
    parts.append(dod)

    parts.append(
        "---\n\n"
        "New here? [`docs/implementation-plan.md`](../blob/main/docs/implementation-plan.md) "
        "explains how to claim and start a task with no prior context.\n\n"
        "_Generated from `docs/tasks.yaml` by `scripts/seed_issues.py`._"
    )
    return "\n\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tasks = {t["id"]: t for t in yaml.safe_load(TASKS.read_text())["tasks"]}
    order = toposort(tasks)
    print(f"{len(tasks)} tasks, dependency graph is acyclic")

    existing_raw = gh(
        "issue", "list", "-R", REPO, "--label", "task", "--state", "all",
        "--limit", "300", "--json", "number,title",
    )
    numbers: dict[str, int] = {}
    for issue in json.loads(existing_raw):
        m = re.match(r"^(T\d+):", issue["title"])
        if m:
            numbers[m.group(1)] = issue["number"]

    # Pass 1 — create anything missing, in dependency order so numbers read naturally.
    for tid in order:
        if tid in numbers:
            continue
        task = tasks[tid]
        title = f"{tid}: {task['title']}"
        if args.dry_run:
            print(f"  would create {title}")
            numbers[tid] = -1
            continue
        labels = ["task", f"wave:{task['wave']}", f"area:{task['area']}"]
        url = gh(
            "issue", "create", "-R", REPO, "--title", title,
            "--body", render_body(task, numbers),
            "--label", ",".join(labels),
        ).strip()
        numbers[tid] = int(url.rstrip("/").split("/")[-1])
        print(f"  created #{numbers[tid]} {title}")

    if args.dry_run:
        print("dry run: no issues created or updated")
        return 0

    # Pass 2 — rewrite every body now that all dependency numbers are known.
    for tid in order:
        gh("issue", "edit", str(numbers[tid]), "-R", REPO,
           "--body", render_body(tasks[tid], numbers))
    print(f"resolved dependency references in {len(order)} issues")

    unresolved = [t for t in tasks.values() for d in t.get("depends_on", []) if d not in numbers]
    if unresolved:
        print(f"WARNING: unresolved dependencies remain: {unresolved}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
