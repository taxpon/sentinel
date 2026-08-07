---
title: Write every artifact in English, regardless of the working language
status: accepted
date: 2026-08-07
type: process
areas: [ops]
tasks: []
files: []
specs: [docs/index.md]
supersedes:
---

# Write every artifact in English, regardless of the working language

## Context

Day-to-day discussion on this project happens in Japanese. The repository is intended to be public
and read by reviewers who do not read Japanese, and the documentation is itself part of what is
evaluated.

## Decision

Everything committed or published is written in English: specs, ADRs, `CLAUDE.md` and rules, slash
commands, GitHub issue and pull request bodies, source comments and commit messages. Conversation
stays in Japanese.

## Alternatives considered

| Option | Why not |
|---|---|
| Japanese artifacts | Unreadable to the intended audience, and the documentation would stop counting as a deliverable |
| Bilingual documents | Doubles the surface that has to stay in sync; the translation goes stale on the first hurried edit |
| English only for the top-level README | The reviewer inspects the whole repository, not the entry point |

## Consequences

Artifacts are directly usable by the audience, and there is one authoritative wording per document
with no synchronisation burden. Writing takes marginally longer than in the working language.

**What would tell us this was wrong:** the audience changing to Japanese-only readers.
