---
title: The sweep files only advisories whose fix forces a decision, at most three a night
status: accepted
date: 2026-08-08
type: process
areas: [remediation]
tasks: [T43]
files: [src/sentinel/scanner/audit.py]
specs: [docs/01-overview.md, docs/05-devin-integration.md, docs/remediation-candidates.md]
supersedes:
---

# The sweep files only advisories whose fix forces a decision, at most three a night

## Context

Resolving `taxpon/superset` against OSV returns **24 findings** across the two dependency trees.
Filing 24 issues would be worse than filing none: each one is labelled `devin:autofix`, so each
starts a session against a 10-ACU `dep-upgrade` budget, and the daily ceiling is 100
([09](../09-operations.md#configuration)). A nightly job that spends the whole budget on version
bumps and puts two dozen issues in front of a maintainer trains everybody to ignore it, and the
issues it files displace the webhook-driven work the pipeline exists for.

[`docs/remediation-candidates.md`](../remediation-candidates.md) had already done this triage by
hand and is the calibration for it. Of the dependency findings it examined it kept two — paramiko
and the deck.gl family's transitive `image-size` — and rejected the rest with reasons that
generalise:

- **flask 2.3.3 → 3.1.3.** Genuinely unpatched, but `pyproject.toml` already permits `<4.0.0`. "The
  lock is simply stale… the fix is already inside the allowed range, so it is a recompile."
- **dompurify 3.4.12 → 3.4.13.** "Patch-only within the same major with no forced call-site
  change."
- **cryptography 49.0.0 → 50.0.0.** Higher severity than paramiko, and rejected anyway because the
  50.0.0 changelog "lists no backwards-incompatible entries."
- **setuptools.** "sdist build-time only, with no runtime call site", and held by a pin whose
  reason is recorded in the tree.
- **xlsx.** Two advisories with no fixed version that can never be satisfied — and the explicit
  warning that the sweep "will report `xlsx` on every single run, forever" without a suppression
  list.

The shape underneath all of those is the same, and it is the definition of the classes being filed
under. [01](../01-overview.md#issue-classes) describes `security-dep` as "a CVE fix in a dependency
**that requires adapting to a breaking API change**" and `frontend-dep` as "an npm advisory **plus
the call-site changes it forces**". An advisory whose fix is a lock regeneration is not an instance
of either class, whatever its severity.

## Decision

**File only where the fix forces a decision beyond regenerating a lock file.** The gates, in the
order they are applied, each reported by name rather than dropping a finding silently:

| Gate | Rejects | Why |
|---|---|---|
| Suppression list | A named advisory against a named package, with a written justification | The `xlsx` case: unsatisfiable by any constraint, and it would recur nightly forever |
| Withdrawn | An advisory OSV has retracted | |
| Build toolchain | `pip`, `setuptools`, `wheel`, `pip-tools`, `virtualenv`, `build`, `twine` | Pinned because `pip-compile` ran, not because Superset ships them. Nothing imports them, so there is no call site to adapt and no regression a test could assert. Five of the 24 findings are `pip` alone |
| No remediation | Neither `fixed` nor `last_affected` | No version satisfies it |
| No manifest line | Nothing declares the package or reaches it | The issue could not say what to change |
| Already filed | [The sweep's memory](./2026-08-08-the-filed-issue-is-the-sweeps-memory.md) | |
| **Lock refresh** | The fix the manifests already permit | Below |
| Per-sweep limit | Everything past the third surviving package | Below |

**The lock-refresh gate** asks the same question two ways, in order of authority. If the repository
declares a constraint for the package and that constraint already admits the fixed version, nothing
in any manifest changes and the remediation is `pip-compile` or `npm install` — rejected. If nothing
declares the package, or the range cannot be read, the fallback is the version boundary: a fix
inside the same major — the same minor below 1.0, which is where every resolver puts the boundary —
is carried by regenerating the lock, and one that crosses it forces every dependent to be checked.

An advisory that states only `last_affected` is treated as having its fix immediately above that
bound, and is filed *with the fact that OSV never published a fixed version stated in the body*.
That is the paramiko case, and it is the difference between an issue Devin can act on and one it
will act on wrongly.

**The per-sweep limit is three.** It is a rate, not a total: the job is nightly, so a real backlog
drains at three a night rather than arriving in one morning, and the findings above the line are
reported as deferred rather than suppressed. Three is the number the rest of the system is already
sized for — `MAX_CONCURRENT_SESSIONS` defaults to 3, and three `dep-upgrade` sessions at their
ceiling is 30 of the 100-ACU daily budget. Advisories on one package are grouped into one issue
before the limit is applied, because one upgrade answers all of them.

On the tree as triaged this leaves **three issues from 24 findings**: `image-size` (both of its
advisories, one issue), paramiko, and cryptography.

## Alternatives considered

| Option | Why not |
|---|---|
| File everything, let the reviewer triage | The failure this task exists to avoid. It also spends the ACU budget before a human sees any of it |
| A severity threshold | Files `dompurify` (Moderate) and `flask`, and drops paramiko, which is rated **Low** and is the strongest dependency candidate in the tree. Severity measures how bad the defect is, not whether fixing it is work |
| Only direct dependencies | Drops `image-size`, which is four levels below anything Superset declares and is the whole of the `frontend-dep` candidate |
| Reachability analysis — does the vulnerable code have a call site? | What [remediation-candidates](../remediation-candidates.md) did by hand for `dompurify` ("no `IN_PLACE` or `addHook` usage"), and the right answer in principle. It needs a call graph across two languages that the sweep does not have and cannot cheaply acquire |
| A suppression list alone, with no general rule | Every rejection becomes a hand-written entry, so the list grows with the tree and the sweep files noise until somebody adds the line. The list is for what a rule cannot express, not instead of one |

## Consequences

A `security-dep` or `frontend-dep` issue from the sweep is one where somebody has to renegotiate a
constraint the project deliberately wrote down. That is what the class promises and what the
`dep-upgrade` playbook is budgeted for.

**Both halves of the gate are proxies, and each is wrong in a knowable direction.**

It *under-rejects*: crossing a major does not prove a call site breaks. `cryptography` 49 → 50 is
exactly this — [remediation-candidates](../remediation-candidates.md) rejected it after reading the
50.0.0 changelog and finding no backwards-incompatible entries, and no scanner can read a
changelog. So it is filed, and the body says which rule filed it and instructs the session to report
`outcome: blocked` with that reason rather than opening a pull request for a pin bump. A false
positive therefore costs one short session and produces a correction, instead of a merged version
bump wearing a remediation's clothes.

It *over-rejects*: a same-major upgrade can break call sites, and one inside a declared range can
too. Those findings are not filed at all. The mitigation is thin — they stay in `skipped` in the
run's report, where nothing acts on them — and the honest statement is that **the fork's stale
lock stays stale**. Keeping dependencies current is a different job from remediating a
vulnerability, and this sweep does only the second.

**What would tell us this was wrong:** most filed issues coming back `outcome: blocked` with "this
was only a pin bump" would mean the gate is too loose and needs the changelog signal it cannot
currently see. A `security-dep` CVE reaching production through a package the gate rejected as a
lock refresh would mean the opposite, and the answer then is a scheduled lock-refresh job — not a
looser filter here, which would drown the class that is working.
