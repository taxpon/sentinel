# Sentinel

**An event-driven remediation pipeline on the Devin API v3.** A maintainer labels an issue on a
repository; Sentinel opens an autonomous Devin session scoped to that issue and drives the resulting
pull request to a merge — re-engaging the *same* session when CI fails or a reviewer requests
changes, and recording every state transition so the result can be measured rather than asserted.

The target repository is [`taxpon/superset`](https://github.com/taxpon/superset), a fork of Apache
Superset. The work it does there is the toil backlog every mature codebase carries: dependency
CVEs, flaky tests, deprecations, N+1 queries — items that are individually small, collectively
large, and perpetually deprioritised.

> **Pull requests are merged within the fork, not into `apache/superset`.** Upstream has its own
> review process and timelines that no demonstration can control, so "merged" throughout this
> repository and its metrics means merged into `taxpon/superset`. The fixes themselves are real
> remediations of real defects in the Superset codebase, not synthetic changes.
> ([blockers.md#b10](./docs/blockers.md#b10))

## What is actually interesting here

Not that a model writes a patch. Three things:

**The unit of delegation is a task, not a keystroke.** Devin is given an issue and a repository and
left to investigate, run the test suite, open a pull request, read its own CI failures and fix them.
Throughput then scales with concurrent sessions rather than with engineer headcount.

**The review-fix loop closes.** A failing check suite or a "changes requested" review resumes the
session that opened the pull request — with the CI output or the reviewer's words — instead of
starting a new one that has forgotten everything. `cycle` counts the laps, and is capped, so a
session that cannot converge escalates to a human instead of burning budget for ever.

**The bottleneck moves, and is measured.** Delegating implementation shifts the constraint to review
capacity. Sentinel instruments precisely that: time-to-PR, review latency, autonomy rate and fix
cycles per merge, all from an append-only transition log rather than from a spreadsheet.

## Architecture

```
GitHub ──webhook──▶  api  ──▶ Postgres ◀── worker ──▶ Devin v3
                      │        (state,     │
                      │         queue,     └── poller ──▶ Devin v3, GitHub
                      │         events)
                      └──▶ dashboard + /api/analytics/*
```

| Process | Responsibility |
|---|---|
| `api` | Verify the HMAC signature, deduplicate the delivery, enqueue work, serve the analytics API and the dashboard. Answers `202` without waiting for any of the work. |
| `worker` | Claim jobs, call Devin and GitHub, apply concurrency, budget and retry policy. Several may run: claiming is `SELECT … FOR UPDATE SKIP LOCKED` under a fenced lease. |
| `poller` | Reconcile Devin session state and GitHub pull-request state into the database. Devin has no outbound webhook for session status, so this is not optional. |
| `db` | Postgres. Durable state, the job queue, and the append-only event log. Everything else is stateless around it. |

Two design decisions carry most of the weight:

- **Postgres is the job queue.** One store for state, queue and audit trail means a transition and
  the job it enqueues commit together — there is no window in which one exists without the other.
- **Deduplication is enforced by constraints, not by code.** `webhook_delivery.delivery_id` is
  unique and so is `remediation (repo, issue_number)`, so a redelivered webhook and a re-labelled
  issue are both no-ops by construction rather than by a check that could be raced.

The full reasoning, and what was rejected, is in [`docs/adr/`](./docs/adr/index.md).

## Quick start

Two paths, with different prerequisites. **Running the stack needs Docker with Compose and nothing
else** — the image installs its own Python environment and builds the dashboard bundle, so the host
needs no Python and no Node. Running the test suite locally needs
[`uv`](https://docs.astral.sh/uv/), which fetches the right Python itself; Node 20+ matters only if
you develop the dashboard outside Docker.

```bash
# Run it — Docker only
cp .env.example .env      # then fill in the four required credentials
make db                   # start Postgres and wait for it
make migrate              # apply the schema
make up                   # api, worker, poller — and the dashboard at /
curl -s localhost:8000/healthz        # pipe through `jq` if you have it
```

```bash
# Test and develop — requires uv, no credentials
make install              # uv sync
make db                   # the tests need Postgres; Devin and GitHub they fake
make ci                   # lint, type-check, and the full test suite
```

`make ci` migrates the test database itself. `make migrate` is for the one `make up` serves.

The suite needs only the database — Devin and GitHub are faked at the HTTP boundary, so `make ci`
passes with no credentials at all. Running it in a second checkout at the same time needs its own
`POSTGRES_PORT` and `COMPOSE_PROJECT_NAME`: two runs sharing one database deadlock each other.

Configuration is environment variables only; `.env.example` is the canonical list and
[`docs/09-operations.md#configuration`](./docs/09-operations.md) explains each one. Four are
required — a Devin token and organisation id, a GitHub fine-grained PAT, and the webhook secret.

Pointing it at a repository for the first time — enabling issues on the fork, creating the label
set, registering the webhook, seeding Devin playbooks and knowledge notes — is scripted:

```bash
make bootstrap            # both halves; each is idempotent and re-runnable
```

## Documentation

[`docs/index.md`](./docs/index.md) is the map. In reading order: the
[overview](./docs/01-overview.md) for the problem, the
[architecture](./docs/02-architecture.md) for the shape, the
[remediation lifecycle](./docs/04-state-machine.md) for the state machine and the review-fix loop,
and [operations](./docs/09-operations.md) to run it.

Three documents track the work rather than the design:

| Document | Read it when |
|---|---|
| [Implementation plan](./docs/implementation-plan.md) | You are picking up work — it explains how parallel sessions divide it |
| [Decision records](./docs/adr/index.md) | Something looks arbitrary and you are about to change it |
| [Blockers & risks](./docs/blockers.md) | Before assuming any part of this works |

**[`blockers.md`](./docs/blockers.md) is not an appendix.** It records what is unresolved, what was
accepted as a limitation, and what evidence each judgement rests on — including the ones that
constrain the demonstration. A pipeline that reports only its successes is not observability, and
the same standard applies to the repository describing it.
