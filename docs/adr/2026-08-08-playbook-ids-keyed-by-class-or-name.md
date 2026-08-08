---
title: Accept DEVIN_PLAYBOOK_IDS keyed by issue class or by playbook name
status: accepted
date: 2026-08-08
type: architecture
areas: [devin]
tasks: [T15, T40]
files: [src/sentinel/devin/playbooks.py]
specs: [docs/05-devin-integration.md]
supersedes:
---

# Accept DEVIN_PLAYBOOK_IDS keyed by issue class or by playbook name

## Context

Playbook CRUD is enterprise-scoped ([B6](../blockers.md)), so the four playbooks are created by hand
in the Devin UI and their ids arrive as configuration. Two statements in the specs describe that
configuration differently. `docs/05-devin-integration.md` maps the session field as
`playbook_id = PLAYBOOK_IDS[issue_class]`, while the playbook table gives **four** playbooks serving
**eight** issue classes. `.env.example` documents `DEVIN_PLAYBOOK_IDS` as a JSON map of issue class
to playbook id, which means eight entries, six of them duplicates of another entry's value.

`.env.example` is owned by T01 and is checked against the configuration table in
`docs/09-operations.md` by `tests/test_env_example.py`, so the wording there is not T15's to change.

## Decision

`playbook_id_for(issue_class, playbook_ids)` looks up the issue class first and falls back to the
name of the playbook that serves it. An operator may therefore configure four entries keyed by
playbook name, eight keyed by issue class, or a mix; an issue-class key always wins over the
playbook-name key.

A class with neither key raises `MissingPlaybookId`, which is deliberately **not** an
`UnknownIssueClass`. An unrecognised class is a property of the issue and escalates to a human as
`QUEUED → BLOCKED` with the reason "issue class unrecognised" ([04](../04-state-machine.md)); a
missing configuration key is a property of the deployment. Conflating them would tell a human that
the class is unrecognised — on every issue of that class — when one environment variable is short an
entry.

## Alternatives considered

| Option | Why not |
|---|---|
| Issue-class keys only, as `.env.example` reads literally | Eight entries where four ids exist. Every playbook id has to be pasted twice, and a typo in one copy silently sends half a class of work to the wrong playbook |
| Playbook-name keys only | Contradicts `.env.example` and the field mapping in the spec, neither of which T15 owns, and removes the ability to point one class at a different playbook |
| Derive ids from a naming convention on the Devin side | Ids are opaque UUID-like strings assigned by Devin; there is nothing to derive them from |

## Consequences

The bootstrap script and the worker read one function instead of interpreting the environment
variable themselves, and the shorter four-entry form stays valid if a later playbook is split across
classes. T40 can surface a missing id at bootstrap, where it is cheap to fix, rather than letting it
appear as an escalated issue at run time. The cost is a resolution rule that is not visible in
`.env.example` — it lives in the function's docstring and in the error message raised when neither
key is present, which names both keys that would have worked.

**What would tell us this was wrong:** an operator configuring both key forms and being surprised by
which one won — that is, the precedence mattering in practice rather than only on paper.
