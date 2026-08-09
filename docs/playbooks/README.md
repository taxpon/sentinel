# Playbook texts

> **Status:** Design · **Answers:** What is in the four Devin playbooks, and how do I change one?

Sentinel creates every session with a `playbook_id` ([05](../05-devin-integration.md)). Playbook
create/update/delete live under `/v3/enterprise/playbooks/*` and need enterprise scope, which this
token does not have ([B6](../blockers.md#b6)), so the four playbooks are **created by hand in the
Devin UI**. That makes these files the only record of what they contain.

**These files are the source of truth.** Devin holds a copy; this directory holds the original. A
change here is not live until somebody re-pastes the block into the Devin UI, and a playbook edited
in the UI without a matching change here is drift nobody can see.

## The four

| Text | Playbook | Classes | `max_acu_limit` |
|---|---|---|---|
| [`security-fix.md`](./security-fix.md) | `security-fix` | `security`, `bug` | 20 |
| [`dep-upgrade.md`](./dep-upgrade.md) | `dep-upgrade` | `security-dep`, `frontend-dep` | 10 |
| [`flaky-test.md`](./flaky-test.md) | `flaky-test` | `flaky-test`, `typing` | 12 |
| [`deprecation.md`](./deprecation.md) | `deprecation` | `deprecation`, `perf` | 12 |

Each file opens with a short header and then a single fenced block. The block is the playbook body,
and nothing outside it is sent to Devin.

The names, caps and class mappings are specified in
[05](../05-devin-integration.md#playbooks-and-acu-caps) and implemented in
[`src/sentinel/devin/playbooks.py`](../../src/sentinel/devin/playbooks.py); a test compares the two.
This directory adds the one thing neither of them holds — the text.

## The four sections

The body is not free prose. Devin's playbook editor expects a fixed structure, supplied by the
repository owner from the Devin UI, and every one of the four texts uses exactly these four headings
in this order:

```
## Overview

Playbook description.

## Procedure

1. **step 1**: explanation

2. **step 2**: explanation

## Advice & Pointers

Helpful facts here

## Forbidden actions

Anything you don't want Devin to do
```

- **Overview** — one paragraph naming the class of defect and what makes it different from the
  other three. Not the objective; the prompt carries that.
- **Procedure** — the ordered things that must be **established** before the work can be finished,
  four to six of them. This is where the format meets the rule that we delegate the task and
  [not the steps](../adr/2026-08-07-delegate-task-not-steps.md), and the resolution is that a
  procedure for a *class* of work states what has to be true before moving on, never what to type.
  "Determine whether the test is intermittent or was never able to
  pass, before changing anything" is a step. "Run `pytest -x tests/foo.py`" is a script, and belongs
  in neither the playbook nor the prompt. A step that would read the same in all four playbooks is
  not class-level standing context and does not belong here.
- **Advice & Pointers** — the facts. Where this kind of defect hides in Superset, what the scoped
  CI will and will not tell you, what `root_cause` has to say for this class, and the ACU budget
  with what it is meant to be spent on.
- **Forbidden actions** — the specific wrong moves this class invites, in the imperative-free form
  of a list of things not to do. Each entry earns its place by being a mistake somebody would
  plausibly make *on this class*; the section is not a place to restate the prompt's constraints.

## What belongs in a playbook

Devin receives three things, and each carries a different kind of fact
([ADR](../adr/2026-08-07-delegate-task-not-steps.md)):

| | Holds | Changes |
|---|---|---|
| **Prompt** | The issue, the objective, the definition of done, the constraints | Every issue |
| **Playbook** | What is true of every issue of this class | When we learn something about a class of work |
| **Knowledge notes** | Repository facts — how to run the suites, lint rules, PR conventions | When the repository changes |

The test for a line in a playbook is whether it would **differ between the four**. A line that would
read the same for a `security` issue and a `flaky-test` issue belongs in the prompt, and the prompt
already says it — putting it here means Devin is told the same thing twice, in two voices.

What is deliberately absent from all four, because the prompt carries it: the objective, that the
branch is cut from and the pull request opened against the target repository's base branch, that a
regression test must fail before the change and pass after it, that generated files and unrelated
modules are out of bounds, and that `blocked` with a specific reason is preferred to forcing a
change.

## Changing one

1. Edit the file here and open a pull request, so the change is reviewed like any other.
2. After it merges, paste the fenced block into the playbook in the Devin UI, replacing its body.
3. Leave the playbook id alone. `DEVIN_PLAYBOOK_IDS` ([09](../09-operations.md#configuration)) maps
   issue classes and playbook names onto ids; creating a new playbook instead of editing the
   existing one silently orphans the configured id and every session of that class fails to start.

Creating them for the first time is part of the Devin bootstrap: create four playbooks with the
names in the table above, paste each body, and record the four ids in `DEVIN_PLAYBOOK_IDS` — keyed
by playbook name, which is enough for all eight classes
([ADR](../adr/2026-08-08-playbook-ids-keyed-by-class-or-name.md)).

## What the texts assume about CI

Each text states which checks a change of its class will actually get, because that is the feedback
Devin receives inside the review-fix loop and it determines what a green run is worth. The workflow
those statements describe is [`../fork-ci/devin-autofix-ci.yml`](../fork-ci/devin-autofix-ci.yml),
which is a **deliberate narrowing** of Superset's CI ([B2](../blockers.md#b2),
[08](../08-testing.md#ci-on-the-fork)). If the workflow's scoping rules change, these four texts
are wrong until they are updated with it.
