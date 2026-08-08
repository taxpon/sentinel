---
title: The policy releases the job it refuses, and decides the terminal verdict first
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline]
tasks: [T21, T23]
files: [src/sentinel/policy/admission.py]
specs: [docs/06-event-pipeline.md, docs/04-state-machine.md]
supersedes:
---

# The policy releases the job it refuses, and decides the terminal verdict first

## Context

Four things can stop a `create_session` job before it calls Devin, and the reliability policy in
[06](../06-event-pipeline.md#reliability-policy) gives each a different ending. The concurrency cap
defers the job without spending an attempt. The ACU budget moves the remediation to `BLOCKED`,
which [04](../04-state-machine.md#escalation) says must escalate rather than terminate quietly — a
state change, a `remediation_event` and an `escalate` job, written together. A remediation that
already carries a `devin_session_id` must not get a second session
([ADR](./2026-08-08-one-claim-statement-and-a-fenced-lease.md): the lease fences the row, not the
work). A remediation that went terminal while its job waited in the queue has nothing left to do.

Only one of the four lets the handler proceed, and each of the other three leaves the job in a
different state. Two of them can also hold at once: an exhausted budget and a saturated queue.

## Decision

`admit_session()` returns a `Decision`, and has already released the job for every verdict except
`ADMITTED` — deferred for the concurrency cap, completed for the three that will never run. The
handler's obligation is one line: `if not decision.admitted: return`. Nothing here commits, so the
refusal, the state change, the event and the escalation are in the handler's transaction with
everything else it writes.

The checks run in a fixed order, terminal verdicts before temporary ones:

1. the remediation is terminal; 2. a session already exists; 3. the daily ACU budget;
4. the concurrency cap.

The two writes that produce a `BLOCKED` remediation are exposed as `block()`, because the budget is
not the only thing that reaches that state.

## Alternatives considered

| Option | Why not |
|---|---|
| Return a verdict and let the handler act on it | The three refusals need four different endings, one of which is a three-row transaction. Spreading that across the two handlers that create and resume sessions is how one of them ends up failing a deferral instead of deferring it — and `defer()` versus `fail()` is precisely the distinction the spec is explicit about |
| Raise an exception per refusal | Three of the four are ordinary outcomes, not errors. An exception also loses the figures the decision was reached on unless each carries them, and a handler that forgets one `except` spends money |
| Check the concurrency cap first, as the cheaper query | It postpones a terminal verdict for as long as the queue stays busy: an over-budget remediation would be deferred every sixty seconds, staying `QUEUED` with no escalation and nothing on the dashboard, while the operator's cost ceiling silently fails to be visible. The ordering costs one consumption call per deferral round |
| Let the handler check for an existing session itself | It is the same check the domain deduplication layer exists for, and the ADR on the fenced lease requires it of every handler with an external side effect. Putting it where the other pre-flight decisions are means one place answers "may this job call Devin?" |

## Consequences

A handler cannot proceed past a refusal by accident, and cannot end a refused job the wrong way:
the only thing it can do with a non-admitted decision is return. `Verdict` is a closed vocabulary,
so a new limit added later is a new member and every caller that matches on it exhaustively is
told. The decision, its reason and the figures behind it are one structured log line.

The cost is that the policy writes to the queue and to the state machine, so it is no longer a
function of its inputs alone: reading `admit_session()` does not tell you what happened to the job
unless you also read what each verdict means. The docstring is written to answer that in the first
ten lines, and every verdict has a test asserting the job's row afterwards.

**What would tell us this was wrong:** a second caller wanting the decision without the writes — a
dry run, a dashboard panel showing what the policy would do — which would mean the evaluation and
the enforcement want separating; or a deferral loop long enough to matter, which would mean the
consumption call in front of it needs caching for the length of a poll cycle.
