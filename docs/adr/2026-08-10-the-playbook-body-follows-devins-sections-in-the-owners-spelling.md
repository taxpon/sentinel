---
title: The playbook body follows Devin's sections in the owner's spelling
status: accepted
date: 2026-08-10
type: process
areas: [devin]
tasks: [T15]
files: []
specs: [docs/playbooks/README.md]
supersedes:
---

# The playbook body follows Devin's sections in the owner's spelling

## Context

Devin's own guidance, [Creating playbooks](https://docs.devin.ai/product-guides/creating-playbooks),
describes an optional set of sections and shows an example using `## Overview`,
`## What's Needed From User`, `## Procedure`, `## Specifications`, `## Advice and Pointers` and
`## Forbidden Actions`. The repository owner separately supplied a template from the Devin UI with
four headings: `## Overview`, `## Procedure`, `## Advice & Pointers`, `## Forbidden actions`.

The two disagree in two ways. The wording differs — `Advice and Pointers` against
`Advice & Pointers`, `Forbidden Actions` against `Forbidden actions` — and the template omits
`Specifications` and `What's Needed From User` entirely. The four playbook texts had followed the
template, and so carried no postconditions at all.

## Decision

Where the page and the template disagree on the **spelling** of a heading, the template wins: it is
what the owner's UI shows, and a text that does not match it reads as drift the next time somebody
compares the two.

Where the page describes a section the template lacks, the section is **added** under the page's own
name. `Specifications` is added to all four texts, holding the
[remediation acceptance criteria](../08-testing.md#remediation-acceptance-criteria) expressed per
class. `What's Needed From User` is omitted: Sentinel creates every session through the API with the
prompt already assembled, so there is no point at which a user could be asked for anything.

The page's rule that a procedure step is one imperative line, with step-specific reasoning nested
beneath it as a sub-bullet, is followed as written. It does not displace
[give Devin the objective and constraints, not the steps](./2026-08-07-delegate-task-not-steps.md):
that record governs the per-issue prompt, and names playbooks as the home for class-level standing
instruction. A step therefore says what to establish — "Determine whether the guard is absent or
merely narrow" — and never what to type.

## Alternatives considered

| Option | Why not |
|---|---|
| Follow the documented page exactly, including its heading wording | Renames the sections the owner's UI presents, for no gain; the wording carries no meaning that the template's does not |
| Keep the template's four sections and drop `Specifications` | Leaves the four texts with no statement of what must be true when the work is done, which is the one thing a class-level text can say that the prompt cannot say generically |
| Add an empty `What's Needed From User` for symmetry with the page | A section that is structurally always empty is noise in a prompt that is charged per token |

## Consequences

The texts are recognisable to anyone reading Devin's documentation while still matching the editor
the owner actually uses, and each class now states its own postconditions rather than leaving them
implicit in the acceptance criteria document. The cost is that the structure is now pinned by two
sources at once, and a change to either has to be reconciled by hand.

**What would tell us this was wrong:** the Devin UI rejecting or silently reformatting a section
name from the owner's template, or a playbook run in which Devin acts on `Specifications` as though
it were a further list of steps rather than a set of postconditions.
