# Sentinel

Event-driven remediation pipeline built on the **Devin API v3**. A labelled GitHub issue on
`taxpon/superset` becomes an autonomous Devin session, a pull request, and a merge — with the same
session re-engaged when CI fails or a reviewer requests changes.

Full documentation: [`docs/index.md`](docs/index.md).

## If you are starting work

Read [`docs/implementation-plan.md`](docs/implementation-plan.md) — the **Cold start** section at the
top tells you how to find, claim and begin a task with no prior context. The session-start hook also
reports the ready tasks automatically.

Do not read every spec. Your issue names the one document it implements; that plus the decision
records it lists is your working set.

## Non-negotiable rules

**1. Every pull request carries tests for the code it changes.**
A change under `src/sentinel/**` without a corresponding change under `tests/**` is incomplete. CI
enforces this. Tests must assert behaviour, not merely that a function was called — for the Devin
client in particular, assert the *captured request body*, because the tags and schemas we send are
part of the contract reviewers verify independently.

**2. UI changes carry UI tests.**
Component tests (Vitest + Testing Library) are mandatory for anything under `dashboard/`: rendering,
prop branches, empty and error states, number formatting. Browser-level tests (Playwright, Cypress,
screenshot comparison) are **out of scope** — do not write them and do not propose them. This does
not apply to `tests/test_e2e.py`, which is in-process and required. See
[`.claude/rules/ui-testing.md`](.claude/rules/ui-testing.md).

**3. Run the PR review toolkit before opening a pull request, and resolve everything it finds.**
Use `/finish-task`, which runs `pr-review-toolkit:review-pr` and records the result. A `PreToolUse`
hook blocks `gh pr create` until that has happened for the current `HEAD`. Pushing more commits
invalidates the marker, so review the final state, not an earlier one.

**4. Record non-obvious decisions as ADRs.**
If you chose between defensible options, or someone will later ask "why is it like this?", write a
record in `docs/adr/` and run `make adr-index`. Criteria and the template are in
[`docs/implementation-plan.md`](docs/implementation-plan.md#adrs). Do not write one for following a
library idiom or for a choice the spec already justifies.

**5. Never modify files outside your task's "Owned files".**
This is the entire collision-avoidance strategy for parallel sessions. Ownership is declared in
[`docs/tasks.yaml`](docs/tasks.yaml). If you need a change elsewhere, say so on the issue rather
than making it.

## Working agreements

- Conversation may be in Japanese. **Everything committed is written in English** — docs, ADRs,
  issue and PR bodies, code comments, commit messages.
- The specs in `docs/` are authoritative. If the code must diverge, change the spec in the same pull
  request and explain why.
- One issue, one branch (`task/T<id>-<slug>`), one pull request containing `Closes #<N>`.
- Work in a dedicated worktree: `git worktree add ../wt/T<id>-<slug> -b task/T<id>-<slug>`.
- Do not delete issues, sessions or history to tidy up. A stalled session left visible is worth more
  than a clean but incomplete trail — this applies to the project's own audit trail too.
- Generated files are never hand-edited: `docs/adr/index.md`, `.claude/rules/adr-pointers.md`.

## Commands

```bash
make test          # pytest against the Compose Postgres
make lint          # ruff + mypy
make adr-index     # regenerate the ADR index and pointer rules
make up            # docker compose up -d
```

Running tests in more than one worktree at once requires a distinct
`COMPOSE_PROJECT_NAME` and `POSTGRES_PORT` per tree.

## Project facts worth knowing

- The target repository's default branch is **`master`**, not `main`.
- Devin has **no outbound webhook** for session status — the poller is how state advances.
- Devin credentials are not yet available ([`docs/blockers.md`](docs/blockers.md), B8). Everything
  through wave 3 is buildable without them because the API is faked with `respx`.
