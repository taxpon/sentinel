---
title: Single-file scripts declare the same Python floor as the project
status: accepted
date: 2026-08-08
type: process
areas: [ops]
tasks: [T06]
files: [scripts/session_context.py, scripts/seed_issues.py]
specs: [docs/implementation-plan.md]
supersedes:
---

# Single-file scripts declare the same Python floor as the project

## Context

The scripts under `scripts/` are PEP 723 single-file scripts: `uv run` reads the interpreter and
dependencies from the header comment, not from `pyproject.toml`. They were written declaring
`requires-python = ">=3.11"` while the package itself requires 3.14 and ruff is configured with
`target-version = "py314"`.

Bringing `scripts/session_context.py` under the formatter exposed the mismatch. Ruff rewrote
`except (OSError, subprocess.TimeoutExpired):` into the unparenthesised PEP 758 form, which 3.14
accepts and every earlier version rejects as a `SyntaxError` — confirmed by running the script under
3.13. The file was still valid by its own declaration and unrunnable by it.

## Decision

The inline metadata of every script this task owns declares `requires-python = ">=3.14"`, matching
`pyproject.toml`. The formatter's target and the script's declared floor are the same number.

## Alternatives considered

| Option | Why not |
|---|---|
| Restore the parentheses | This is the formatter, not a lint rule: there is no `# noqa` for it, and the next `make format` would undo the edit |
| Lower ruff's `target-version` | It would give up 3.14 idioms across `src/` and `tests/` to preserve a floor nothing actually runs |
| Keep the scripts excluded from ruff | Removing that exclusion is what this task was asked to do, and an unformatted file is how the mismatch stayed invisible |

## Consequences

The scripts run on the interpreter the rest of the repository already requires, and `uv run --script`
will refuse rather than fail with a syntax error if only an older one is available. `make format` is
now safe to run over them.

`scripts/gen_adr_index.py` still declares `>=3.11` and belongs to another task. It contains no
3.14-only syntax today, but it is formatted by the same configuration, so the next reformatting that
touches an affected construct will put it in the same state.

**What would tell us this was wrong:** needing to run these scripts somewhere that cannot provide
3.14 — a CI image pinned lower, or a contributor's system Python — at which point the floor has to
come down and ruff's target with it.
