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

The suite talks to a real Postgres and has no in-memory fallback: in a worktree where `make db` has
never been run, the database tests **error during setup**. That is the first thing to check when a
fresh tree reports twenty failures at once.

```bash
export COMPOSE_PROJECT_NAME=sentinel-t14   # anything unique to this worktree
export POSTGRES_PORT=54314                 # a free port per worktree
export API_PORT=8014                       # only needed for `make up`
make db
export DATABASE_URL=postgresql+asyncpg://sentinel:sentinel@localhost:54314/sentinel

make test                       # full suite
uv run pytest tests/test_x.py   # one file
```

All three exports are per-worktree: `COMPOSE_PROJECT_NAME` separates the containers and volumes,
`POSTGRES_PORT` and `API_PORT` separate the published host ports. `DATABASE_URL` is what the tests
read — left unset they use port 5432, which is another worktree's database or nothing at all.

## Do not

- Do not weaken an assertion to make a test pass. Fix the code, or fix the spec and say why.
- Do not add `# type: ignore` or `# noqa` without a comment giving the reason.
- Do not test private helpers directly when the public behaviour covers them.
