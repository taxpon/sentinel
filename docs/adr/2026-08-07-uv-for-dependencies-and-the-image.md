---
title: Manage dependencies with uv and a committed lock file
status: accepted
date: 2026-08-07
type: process
areas: [ops]
tasks: [T01, T05]
files:
  - pyproject.toml
  - uv.lock
  - Dockerfile
specs:
  - docs/09-operations.md
supersedes:
---

# Manage dependencies with uv and a committed lock file

## Context

Work on this repository is split across parallel sessions that each run `make test` in their own
worktree, and the same dependency set has to be installed inside the container image. Three
environments therefore have to agree: a contributor's machine, CI, and the image. Python offers no
default answer — `pip` + `requirements.txt`, Poetry, PDM and uv are all in common use — and the
scripts under `scripts/` already use uv's PEP 723 inline metadata to run without a project
environment at all.

## Decision

`pyproject.toml` plus a committed `uv.lock`, driven by uv. `.python-version` pins 3.12, matching the
`python:3.12-slim` base image. The image installs from the lock with `uv sync --locked`, in two
layers: dependencies first from `pyproject.toml` and `uv.lock` alone, then the project. `make`
targets wrap uv so no one has to remember to activate a virtualenv.

## Alternatives considered

| Option | Why not |
|---|---|
| `pip` + `requirements.txt` | No lock of transitive dependencies without adding pip-tools; a second tool for what uv already does |
| Poetry | Equivalent outcome, but the repository would then use Poetry for the project and uv for `scripts/`, so contributors must know both |
| PDM | Same objection as Poetry, with a smaller ecosystem |
| No lock file, resolve at build time | The image and the developer machine drift silently, which is exactly the failure a parallel-session workflow makes hardest to notice |

## Consequences

Installs are reproducible and fast, and the dependency layer of the image is cached until
`pyproject.toml` or `uv.lock` changes. `uv.lock` is a shared file: every task that adds a dependency
touches it. Conflicts are resolved by taking either side and re-running `uv lock`, never by hand.

Contributors and CI need uv on the PATH; the container does not, beyond the build.

**What would tell us this was wrong:** `uv.lock` conflicts stop being mechanical — if resolving one
requires understanding what another task did, the lock has become a coordination point and the
dependency set should be split per component instead. Equally, if uv's resolver disagrees with what
the image actually installs, the single-lock premise has failed.
