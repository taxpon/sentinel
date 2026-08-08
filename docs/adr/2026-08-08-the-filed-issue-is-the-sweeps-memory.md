---
title: The filed issue is the sweep's memory, and a closed one is a decision
status: accepted
date: 2026-08-08
type: process
areas: [remediation, github]
tasks: [T43]
files: [src/sentinel/scanner/audit.py]
specs: [docs/05-devin-integration.md, docs/03-data-model.md]
supersedes:
---

# The filed issue is the sweep's memory, and a closed one is a decision

## Context

The sweep runs every night against a tree that changes slowly, so it meets the same advisories
again and again. [05](../05-devin-integration.md#scheduled-sweep) states the requirement as "do not
open duplicates", which leaves two questions open, and getting either wrong is the failure that
gets a bot switched off:

- **What makes two findings the same one?** OSV publishes a single defect under several
  identifiers. paramiko's SHA-1 flaw is `GHSA-r374-rxx8-8654`, `PYSEC-2026-2858` and
  `CVE-2026-44405`, and `POST /v1/querybatch` returns two of those as separate records for the same
  package. Which one is "the" identifier also changes over time: a CVE and a PYSEC record often
  exist before the GitHub advisory does.
- **Where is "already filed" remembered?** Sentinel has a database, and the obvious move is a
  table. But `remediation` is keyed on `(repo, issue_number)` and only comes into existence when an
  issue is *labelled* — it cannot answer "has this advisory ever been filed" for an issue nobody
  labelled, or one a maintainer closed on sight.

## Decision

**Every issue the sweep files carries its own fingerprints, and reading them back is the whole of
the deduplication.** At the bottom of the body:

```
<!-- sentinel-advisory: PyPI/paramiko/GHSA-r374-rxx8-8654 -->
```

One line per advisory the issue answers — an upgrade that closes three advisories on one package
claims all three, so the siblings are recognised rather than arriving one a night.

Before deciding anything, the sweep lists the issues carrying the two labels it files under —
`class:security-dep` and `class:frontend-dep` — with `state=all`, and collects every marker. A
finding is already filed when the marker set contains its ecosystem, its package and **any** of the
identifiers its OSV record is published under. Filing under `PYSEC-2026-2858` on one night and
resolving to `GHSA-r374-rxx8-8654` on the next is therefore one finding, not two.

**A closed issue counts.** A maintainer who closed one made a decision, and re-filing it tomorrow
would overrule that decision on a schedule. Closing is the off switch, and it is the one every
maintainer already knows how to reach for.

The state lives on GitHub, not in Postgres, for three reasons. The issue is the artefact everyone
shares — the sweep, a maintainer, and a future Devin session all see the same object, where a table
is visible only to Sentinel. A maintainer who wants to know why the sweep stopped filing something
can find the answer on the issue itself. And the sweep holds no state that could disagree with the
repository: there is no row to become stale when an issue is deleted, transferred, or filed by hand
before the sweep ever saw the advisory.

## Alternatives considered

| Option | Why not |
|---|---|
| A `scanner_finding` table | Needs a migration in files T43 does not own, and re-introduces exactly the drift this avoids: an issue deleted or closed by a maintainer leaves a row saying the advisory is handled when nothing on the repository says so |
| Match on the issue title | Maintainers edit titles as triage. A rename would produce a duplicate, and the marker costs nothing to carry |
| GitHub's search API (`is:issue GHSA-…`) | A separate and much tighter rate limit, and it matches any *mention* — a comment saying "this looks like GHSA-x" would suppress a real finding |
| Only the preferred identifier in the fingerprint | The preferred identifier changes when a GHSA record appears for an advisory previously known only as a PYSEC or CVE. That is a routine event, and it would produce a duplicate every time |
| Open issues only | The cheapest way to build a bot that argues with a maintainer indefinitely |
| Reopen the closed issue instead of skipping it | Same objection, with an extra notification |

## Consequences

Running the sweep twice on an unchanged tree files nothing, which is what makes a failed run safe
to leave to the schedule rather than retry in place — so the client that files issues carries no
retry policy of its own.

An issue whose body is edited to remove the marker will be filed again. That is a deliberate escape
hatch rather than a defect: a maintainer who wants the sweep to reconsider something has a way to
say so, and the marker is an HTML comment, so it is invisible in the rendered issue unless somebody
goes looking.

The suppression is only as good as the labels. An issue the sweep filed and somebody re-labelled
out of both dependency classes becomes invisible to the next run and will be filed again. Listing
by label rather than reading every issue on the repository is what keeps the lookup bounded on a
fork that also carries the six other classes and everything a human filed, and the trade is worth
naming rather than discovering.

**What would tell us this was wrong:** a duplicate on the repository. There is no partial version
of this failure — either the fingerprint round-trips or the maintainer sees the same advisory
twice, which is why `tests/test_scanner.py` exercises the second run against the issues the first
run actually wrote rather than against a fixture.
