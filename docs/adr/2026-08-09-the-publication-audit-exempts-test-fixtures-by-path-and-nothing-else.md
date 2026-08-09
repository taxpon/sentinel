---
title: The publication audit exempts test fixtures by path and nothing else
status: accepted
date: 2026-08-09
type: process
areas: [ops]
tasks: [T60]
files: [scripts/audit_history.py, tests/test_audit_history.py]
specs: [docs/09-operations.md, docs/blockers.md]
supersedes:
---

# The publication audit exempts test fixtures by path and nothing else

## Context

`scripts/audit_history.py` mechanises the "Before making the repository public" checklist so that it
can be re-run after any later change, and it has to gate on its exit status to be worth anything.
Two of its four checks match things that are in the history today and will stay there, because
history is append-only:

- `tests/conftest.py`, `tests/test_logging.py` and four other test files carry strings shaped like a
  Devin token, a GitHub PAT and a webhook secret. They have to: the redaction code under test is
  written against those shapes. 53 matches across 6 files, none of them real.
- The brief check compares six-word windows of `requirements/` and `CLAUDE.local.md` against every
  added line and every commit message. Six words of ordinary English collide by chance — a brief
  written in the same register as the documentation contains ordinary sentences, and a run against
  this repository produces such a collision — and a colliding phrase cannot be taken back out of
  the history once it is in it. No example is quoted here for the obvious reason.

So a run that failed on every match would be red forever, on the first day, and would be ignored
within a week. A run that suppressed matches by pattern would be green for exactly the reason that
makes it useless: the pattern that hides a fixture also hides a leak of the same shape.

## Decision

The audit fails on a credential-shaped string only when it lives outside `tests/`, and fails on
every brief-phrase match without exception. Both kinds are always listed with their path and commit,
whether or not they fail the run. There is no allow-list, no baseline file, and no flag that turns a
finding off.

The asymmetry is the point. `tests/` is a statement about the *repository*, not about the match: a
string in the test suite is a fixture because of where it is, which is a fact the audit can check
and a reviewer can re-check, and one that a real credential cannot acquire by accident. Nothing
comparable is true of prose, so a phrase match is handed to a human with the six matched words —
and only those six — printed for them to judge.

## Alternatives considered

| Option | Why not |
|---|---|
| Fail on every match, including fixtures | Permanently red from day one; the fixtures cannot be removed without rewriting history, and they are load-bearing for the redaction tests. A gate nobody can turn green is a gate nobody reads. |
| A baseline or `.auditignore` of acknowledged findings | Keyed on the finding, so acknowledging a phrase means storing that phrase — which puts brief text into the repository the audit exists to keep it out of. Storing hashes instead removes the leak but also removes the reviewer's ability to see what was waved through. |
| Score phrases for "distinctiveness" and fail only on distinctive ones | No honest way to compute it offline. Every proxy considered (stop-word lists, rarity in the repository's own text) decides for the human, silently, on the one judgement the audit exists to put in front of them. |
| Fail only when several overlapping windows hit the same file | Discriminates well against coincidence, but demotes a single verbatim six-word quotation of the brief to a note — and one sentence of the brief is exactly the leak B13 is about. |
| Print the matched phrase with its surrounding brief text, for context | The six matched words are already in the public history; the sentence around them is not. Printing it would put brief text in a terminal, a CI log and possibly a pasted issue comment. |

## Consequences

The audit is green on a repository whose only credential-shaped strings are fixtures, which is the
ordinary state, so it is usable as a release gate and as a re-run after any change. Adding a token
fixture outside `tests/` turns it red — deliberately: a fixture that needs to live elsewhere is a
decision worth stating in a pull request.

A phrase match keeps the run red until a human acts, and the only actions available are to reword
the offending text and rewrite the history, or to decide the collision is innocent and accept a
non-zero exit on every subsequent run. That is a real cost, accepted knowingly: the checklist item
is "the brief has not leaked", and no exit code should be able to say that on a human's behalf.

**What would tell us this was wrong:** operators start passing over the phrase check — running the
script and reading past a known-red check 1 rather than acting on it. At that point the check has
stopped being a gate and become noise, and the missing piece is a reviewed record of accepted
collisions rather than a looser rule.
