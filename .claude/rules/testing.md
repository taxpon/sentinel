---
paths:
  - "src/sentinel/**/*.py"
  - "tests/**/*.py"
  - "scripts/**/*.py"
---

# Testing rules for Python code

**Every pull request that changes `src/sentinel/**` changes `tests/**`.** CI fails otherwise. This
is rule 1 in `CLAUDE.md`; the detail below is what "correctly implemented" means here.

## What a good test asserts

- **Behaviour, not invocation.** Asserting that a function was called proves nothing. Assert the
  value it returned, the row it wrote, or the request body it produced.
- **For the Devin and GitHub clients: assert the captured request.** With `respx`, inspect
  `request.content` and check the path, the tag set, `structured_output_schema`, `max_acu_limit` and
  `resumable`. Reviewers verify these independently in the Devin dashboard, so a test that only
  checks the response shape does not cover the part that matters.
- **Failure paths, not just the happy path.** Every retry policy, every deny branch, every
  `blocked` outcome in the spec has a test.
- **Table-driven where the spec is a table.** The state machine and the event mapping are specified
  as tables in `docs/`; iterate them rather than hand-picking a few rows.

## Fixtures and doubles

- Postgres is real, from Compose. SQLite cannot emulate `FOR UPDATE SKIP LOCKED`, which the queue
  depends on.
- Devin and GitHub are faked with `respx`. Never call either service from a test.
- Use the shared fixtures in `tests/conftest.py` and the builders in `tests/factories.py` rather
  than constructing models inline.
- Recorded GitHub webhook payloads live in `tests/fixtures/github/`. Add to them rather than
  hand-writing partial payloads.

## Running

```bash
make test                       # full suite
uv run pytest tests/test_x.py   # one file
```

Two worktrees running tests at once need distinct `COMPOSE_PROJECT_NAME` and `POSTGRES_PORT`.

## Do not

- Do not weaken an assertion to make a test pass. Fix the code, or fix the spec and say why.
- Do not add `# type: ignore` or `# noqa` without a comment giving the reason.
- Do not test private helpers directly when the public behaviour covers them.
