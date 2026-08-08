---
title: Pin the dashboard toolchain to versions that run on Node 20
status: accepted
date: 2026-08-08
type: process
areas: [dashboard, ops]
tasks: [T30]
files: [dashboard/package.json, dashboard/package-lock.json]
specs: [docs/07-observability.md]
supersedes:
---

# Pin the dashboard toolchain to versions that run on Node 20

## Context

The `dashboard` job in `.github/workflows/ci.yml` runs `npm ci`, `npm run build` and `npm test` on
`node-version: 20`. That is the only automated check the dashboard has.

At the time of writing, the current releases of the obvious dependencies have moved past it:

- `jsdom@30` declares `node: ^22.22.2 || ^24.15.0 || >=26`, so it does not support Node 20 at all;
- `jsdom@28` and above reach `require()` of an ES module through their own dependencies, which only
  Node ≥20.19 and ≥22.12 support — every Node between 22.0 and 22.11 fails at import time;
- `vite@8` builds with Rolldown, whose native binding is an optional dependency gated on the same
  Node range and is silently skipped on anything older, leaving a build that cannot start.

None of these are visible from `package.json`. Each surfaces as a crash inside `node_modules` during
`npm test` or `npm run build`.

## Decision

The toolchain is pinned to the newest versions that build and test on Node 20: `vite@^7`,
`@vitejs/plugin-react@^5`, `jsdom@^26`, with `vitest@^4`, React 19 and Recharts 3 on top.
`engines.node` records `^20.19.0 || >=22.12.0`, which is what Vite itself requires.

Raising any of these is a deliberate change made together with the Node version in the CI job, not a
routine dependency bump.

## Alternatives considered

| Option | Why not |
|---|---|
| Take the latest of everything and raise CI to Node 24 | The Node version in CI is not T30's to choose, and `.github/workflows/ci.yml` is not T30's file. A dashboard that only builds on a Node the repository has not adopted is worse than one a version behind |
| Latest versions with the engine warnings ignored | The failures are not warnings. A missing Rolldown binary and a `require()` of an ESM module are both hard crashes |
| No upper bound, and let the lock file decide | `npm ci` installs the lock file, so CI would stay green while a fresh `npm install` on someone's machine would not — the failure would land on whoever next updated a dependency |

## Consequences

`npm ci && npm run build && npm test` works on Node 20 in CI, on Node 22 locally, and inside the
`node:20-slim` stage of the `Dockerfile`, from a clean checkout. The cost is that the dashboard runs
one major version behind on Vite and several on jsdom, and that the panel tasks inherit those
versions.

**What would tell us this was wrong:** needing a fix or a feature that only exists above these pins.
The answer then is to raise the Node version in the CI job first, in the task that owns that file,
and let the pins follow — not to bump the pins and hope Node 20 copes.
