# Sentinel — Technical Documentation

> **Status:** Design · **Answers:** Where is the specification for X?

Sentinel is an event-driven remediation pipeline built on the **Devin API v3**. A maintainer labels
an issue on [`taxpon/superset`](https://github.com/taxpon/superset); Sentinel verifies the signed
webhook, opens an autonomous Devin session scoped to that issue, and drives the resulting pull
request to a merge — re-engaging the *same* session when CI fails or a reviewer requests changes.
Every state transition is recorded, so the system can answer the question an engineering leader
actually asks: *is this working, how fast, at what cost, and where does it fail?*

## Reading order

| # | Document | Answers | Audience |
|---|---|---|---|
| 01 | [Overview](./01-overview.md) | What problem does this solve, and why is Devin the right primitive? | Everyone |
| 02 | [Architecture](./02-architecture.md) | What are the components, how does an event flow through them, and why is it built this way? | Engineers |
| 03 | [Data model](./03-data-model.md) | What is persisted, and what guarantees do the constraints give us? | Engineers |
| 04 | [Remediation lifecycle](./04-state-machine.md) | What states can a remediation be in, what moves it, and how does the review-fix loop work? | Engineers |
| 05 | [Devin integration](./05-devin-integration.md) | Exactly which v3 endpoints and features are used, and how are prompts, playbooks and tags constructed? | Engineers, reviewers |
| 06 | [Event pipeline](./06-event-pipeline.md) | How are webhooks authenticated, deduplicated, queued and retried? | Engineers |
| 07 | [Observability](./07-observability.md) | How is each metric defined, and what does the dashboard show? | Engineers, leadership |
| 08 | [Testing](./08-testing.md) | What is tested, at which layer, and what counts as evidence? | Engineers |
| 09 | [Operations](./09-operations.md) | How do I configure, bootstrap, run and demo the system? | Operators |

Alongside the specs, three living documents track the work itself:

| Document | Answers | Read it when |
|---|---|---|
| [Implementation plan](./implementation-plan.md) | What is left to build, in what order, and how do parallel sessions divide it? | **Start here if you are picking up work** |
| [Decision records](./adr/index.md) | Why is it built this way, and what was rejected? | Before changing something that looks arbitrary |
| [Blockers & risks](./blockers.md) | What is unresolved, and what is needed to resolve it? | Before assuming something works |

The machine-readable task graph behind the plan is [`tasks.yaml`](./tasks.yaml). GitHub issues, the
session-start hook and the plan's tables are all derived from it.

## Document conventions

Every document opens with a status line:

```
> **Status:** <Design | Implemented | Living> · **Answers:** <the one question this document answers>
```

| Status | Meaning |
|---|---|
| **Design** | Specification agreed before implementation. Code is expected to match it; if it diverges, the document is wrong and gets updated. |
| **Implemented** | Verified against shipped code. |
| **Living** | Continuously updated during implementation. Only [`blockers.md`](./blockers.md) carries this status. |

Facts belong in exactly one document. Where another document needs them, it links rather than
restates.

## Related

- `README.md` — project summary and quick start (not written yet; task T44)
- [Devin API reference](https://docs.devin.ai/api-reference/overview) — upstream source for every API claim in [05](./05-devin-integration.md)
