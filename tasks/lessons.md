# Lessons

Patterns worth not repeating. One entry per mistake, with the rule that prevents it.

## Never run two pytest sessions against the same database at once

The suite empties the schema between tests with `TRUNCATE … RESTART IDENTITY CASCADE`, which needs
an `AccessExclusiveLock` on every table. Two runs sharing a database deadlock against each other:
one holds a lock the other's `TRUNCATE` wants, and each run reports a different, shifting set of
failures — assertions about state the *other* run truncated away, plus outright
`DeadlockDetectedError`.

It looks exactly like flaky application code, and it is not. I spent a long stretch on T22 chasing
a leaked connection that did not exist, because the failing runs were ones I had started twice on
port 54322 myself.

**Rule:** before running the suite, check nothing else is running it —
`pgrep -f pytest` — and give every worktree its own `POSTGRES_PORT`. A run that takes minutes
instead of a minute is the tell: it is waiting on locks, not working.

## A rebase that stops on a conflict leaves the worktree detached

`git rebase` returning non-zero and `git status` saying "interactive rebase in progress" means HEAD
is detached at the partially replayed commit. Verifying there, or pushing from there, tests and
publishes the wrong tree — and the push silently sends the *un*-rebased branch ref instead.

**Rule:** after any rebase, assert `git rev-parse --abbrev-ref HEAD` is the branch name and
`git diff --name-only --diff-filter=U` is empty *before* running tests or pushing. Worktrees do not
keep rebase state at `.git/rebase-merge`, so checking for that path is not a reliable test.

## "Keep both sides" resolves markdown tables, not code

Resolving a conflict by unioning the two sides works for an append-only list — a markdown table of
decisions, a bullet list. Applied to source it produces syntactically broken output: on
`dashboard/src/fixtures/summary.ts` it dropped a closing brace because both sides shared the `/**`
line above it, and the root `make ci` did not catch it because the Python suite never runs
`npm run build`.

**Rule:** union-resolve prose and tables. Resolve code by reading both sides. Either way, verify
against the check that actually covers the file, not the nearest one.

## A mutation test in a copied worktree can import the original

An editable install leaves a `.pth` file pointing at the source tree it was created in. Copying a
worktree copies `.venv` with it, so the copy's tests import the *original* `src/` and every mutant
appears to survive. I reported "the mutation survives" three times on T03 before checking
`m.__file__` and finding it pointed outside the copy.

**Rule:** in a scratch copy, `rm -rf .venv`, `uv sync`, and run with `PYTHONPATH="$PWD/src"` ahead.
If a mutant survives, print the module's `__file__` before believing it.

## Moving major tags on GitHub Actions are not guaranteed to exist

`astral-sh/setup-uv@v9` does not resolve, though `v9.x` releases exist — the action publishes no
`v9` tag. Every job died at "Set up job", and `actionlint` does not check this.

**Rule:** before bumping an action's major, confirm the tag itself resolves:
`gh api repos/<owner>/<repo>/git/ref/tags/v<N>`.
