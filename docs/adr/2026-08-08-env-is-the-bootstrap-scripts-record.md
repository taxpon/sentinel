---
title: .env is the bootstrap script's record of what it created in the Devin organisation
status: accepted
date: 2026-08-08
type: process
areas: [devin]
tasks: [T40]
files: [scripts/bootstrap_devin.py]
specs: [docs/09-operations.md]
supersedes:
---

# .env is the bootstrap script's record of what it created in the Devin organisation

## Context

`docs/09-operations.md#bootstrap` says every step of `make bootstrap-devin` is idempotent. Two of
the four are not idempotent on their own: `POST /v3/organizations/{org}/knowledge/notes` and
`POST /v3/organizations/{org}/schedules` create something new each time they are called. A second
run — after a rotated token, after an edited note, after a failure part-way through — would leave
eight knowledge notes and two nightly sweeps, and two sweeps means the same issues filed twice
every night.

Nothing on Devin's side prevents it. The endpoint table in `docs/05-devin-integration.md` is
closed — `devin/client.py` implements it "no more and no fewer", and a test compares the two in
both directions — and it contains no endpoint that lists an organisation's knowledge notes or its
schedules. There is therefore no server state to reconcile against, and no credentials to discover
one with anyway ([B8](../blockers.md)).

What does exist is one variable the spec already assigns to this script: `.env.example` documents
`DEVIN_KNOWLEDGE_IDS` as "JSON array, written by `make bootstrap-devin`". The schedule has no such
variable.

## Decision

**`.env` is the record.** The script creates only what `.env` does not already account for:

- `DEVIN_KNOWLEDGE_IDS` is **positional** — the *n*-th id belongs to the *n*-th entry of `NOTES` —
  so a run that stopped after two notes resumes at the third. `NOTES` may be appended to and edited
  in place, never reordered.
- `DEVIN_SCHEDULE_ID` is written for the sweep. It is not configuration and no `Settings` field
  reads it; `Settings` is configured `extra="ignore"`, so it rides along in the same file without
  becoming one. That is also why it is **not** added to `.env.example` or to the configuration
  table in `docs/09-operations.md`: those two lists are the process's configuration, they are
  checked against each other by `tests/test_env_example.py`, and `.env.example` belongs to T01.
- The record is written **after each creation**, not once at the end. An id that was not recorded
  belongs to a note nobody can find again.

`.env` holds real credentials, so how it is written is part of this decision:

- one assignment line is replaced, in place — the **last** one for that name, which is the one
  `dotenv` reads. Comments, blank lines, ordering, line endings, duplicates and the operator's own
  variables all survive byte for byte;
- the new text lands through a temporary file in the same directory and `os.replace`, so an
  interruption leaves the previous file complete rather than a truncated new one. The temporary is
  named `.env.*`, which `.gitignore` already covers;
- the file's mode is carried over, so a `.env` chmodded to 0600 stays 0600;
- an id that cannot be written into an unquoted value — whitespace, `#`, a quote, a backslash — is
  **refused**, and handed to the operator in the error instead. A corrupted `.env` is worse than a
  failed bootstrap;
- a missing file is not created. A `.env` holding nothing but one recorded id would look like a
  configuration while missing every credential;
- nothing prints the file, and no method returns more of it than the one variable asked for.

## Alternatives considered

| Option | Why not |
|---|---|
| Reconcile against Devin before creating | No v3 endpoint lists notes or schedules. Adding one would put a path in `ENDPOINTS` that the spec's table does not have, which a test rejects — and inventing an endpoint we cannot call is not evidence of anything |
| A separate state file beside `.env` | A second file to write, git-ignore and explain, when `.env` already holds one of the two ids by the spec's own instruction |
| Treat a duplicate-name `409` from `POST /schedules` as "already exists" | v3 documents no such behaviour and no credentials exist to observe it (B8). If the guess is wrong the failure is silent: a second sweep, discovered a night later |
| Match notes by their `name` | Needs a listing endpoint, which is the same dead end |
| Rely on the operator not running it twice | The spec asks for idempotence, and the run most likely to happen twice is the one that failed part-way through |
| Read the record from `Settings` instead of `.env` | `Settings` has no field for the schedule id, and reading the two records two different ways is one rule more than the script needs. Reading `.env` also makes the round trip real: what the script wrote is what the next run reads |

## Consequences

The record is only as good as `.env`. An operator who deletes it, or points `--env` somewhere else,
gets a second set of notes — the script says so in the error it raises when the file is absent.
Moving to a different Devin organisation means clearing both variables first; nothing checks that
the recorded ids belong to `DEVIN_ORG_ID`.

Because the knowledge ids are positional, reordering `NOTES` would silently attach existing ids to
different notes. The list is documented as append-only for that reason, and the test that counts
the notes against the spec's numbered list fails if the two drift.

`DEVIN_SCHEDULE_ID` is undocumented in `.env.example` until T01 adds it, if it ever should be. An
operator reading `.env` finds a variable the canonical list does not mention.

**What would tell us this was wrong:** v3 exposing a listing endpoint for knowledge notes or
schedules, or an idempotency key on creation. Either would move the record to where it belongs —
Devin's side — and reduce `.env` to a cache.
