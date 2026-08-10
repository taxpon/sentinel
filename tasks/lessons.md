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

## Read the endpoint's own page, not the index

An API's index — `llms.txt`, a route list, a summary table — tells you a path exists. It does not
tell you what the path takes, what it returns, or whether the vendor has stopped recommending it.
Four separate defects here came from trusting something second-hand instead of opening the page:

- The index listed the schedules endpoint with no hint of deprecation. Its own page carries a banner
  saying to use a different feature for any *new* scheduled workflow. We built one, then removed it.
- The page slug said `organizations-tags`; the OpenAPI block inside the same page said
  `enterprise/organizations`. The slug is the URL somebody chose, not the path the API serves — and
  the two disagreed.
- Twice a request or response shape was taken from this project's own design document rather than
  from the reference. One of them sent a field name the API does not define, so the request would
  have been rejected outright. The other omitted a field that switches on the review-fix loop, which
  fails *silently*: the pipeline would have looked healthy and simply never resumed a session.

The last one is the reason this is a lesson rather than an anecdote. It shipped through a green test
suite, because the fakes were built from the same design document as the code. **A test double
copied from the same source as the code under test asserts that you were consistent, not that you
were right.**

**Rule:** open the reference page for the exact endpoint before writing the call, and again before
believing a shape a test asserts. Read three things on it: the request schema, the response schema,
and any banner or callout above them. Where the index and the page disagree, the page wins; where
the page and your own spec disagree, the page wins and the spec gets fixed in the same change.
Anything left unverified says so in the code, next to the field it is unsure about.

## Where the reference is silent, read the code that will actually run

The page above the code is the first source, not the last one. SQLAlchemy's asyncpg dialect
documents no SSL handling at all — and a fetched summary of that page confidently described
`ssl=require` and `ssl=prefer` as though it did, because those values are real in asyncpg and the
summary joined them up. `grep -n ssl` over the installed dialect returns nothing: it forwards every
URL query parameter verbatim as a keyword argument and arbitrates none of them, which is a fact only
the source states.

That mattered because two libraries each did half the job. asyncpg owns a `sslmode` -> `ssl` rename,
but it lives in its DSN parser, and SQLAlchemy never hands asyncpg a DSN — so a URL every provider
issues reached a driver with no idea what `sslmode` meant, and a deploy died on it.

**Rule:** when the documentation does not answer the question, read the installed source and say
which file and line settled it. Where a value transcribes someone else's signature or enum, add a
test comparing it to the real thing — a transcription nobody checks goes stale in the direction that
rejects input the user was entitled to write.

**And be honest about what a local test proves.** This deploy failure could not be reproduced
against the Compose database: that server declines TLS politely and the connection falls back, while
the deployed one accepts and then resets, which is not recoverable. Both passed locally. A green
suite was evidence about our code, not about the connection — say so in the test rather than letting
the tick imply more.

## A fixture that carries the design's assumption cannot test it

Every CI fixture in this repository put **one** check suite on a head SHA. Under that shape, reading
a `check_suite.completed` conclusion as the CI verdict is correct — so 1,800 tests were green while
the pipeline did both of these on its first live run:

- reported CI green off `Hold Label Check`, a workflow that checks for a `hold` label, thirteen
  seconds after the pull request opened and three and a quarter minutes before the suite that judges
  the diff had finished;
- spent a fix cycle resuming Devin against `Dependency Review`, which fails on every pull request in
  that fork for a repository-settings reason, with a resume message built from that workflow's log.

`taxpon/superset` puts 27 suites on a SHA. The number is not the point. The point is that the
fixture was built from the same sentence of `docs/04-state-machine.md` as the code, so the suite
could only ever confirm that the two agreed with each other.

This is the third entry here with that shape — the Devin request bodies asserted against our own
design document, the mutation tests importing the original tree, and now this. The tell differs
every time and the structure does not: **the test and the code share a premise, so the premise is
the one thing the test cannot see.**

**Rule:** when a fixture stands for something outside the system, take its shape from outside — one
real `gh api` call, pasted in with the command that produced it. Then write down the premise it
encodes ("one suite per SHA", "one failing run per commit"); if you cannot name the premise you have
not found it yet. A fixture containing exactly one of anything deserves a second look, because
cardinality is where this hides.

## Never rewrite history in a worktree somebody else is reading

Splitting one commit into two meant, for a few minutes, editing the working tree by hand: remove the
second change, commit the first, restore the second, commit that. A reviewer ran `make ci` in the
same directory during those minutes and saw two tests fail — the merge feature's own tests, against
a `_stamp` that did not stamp. They quoted the code and the line numbers, and both were real.

That state existed in **no commit**. `git show <sha>:src/sentinel/api/webhooks.py` was correct at
every SHA on the branch, before and after the split, and `git diff` between them was empty. What
they had read was the surgery: the half-second window in which the feature's code was removed and
its tests were not yet.

It is a nasty failure to receive, because everything about it looks like a genuine defect —
plausible failures, real line numbers, a coherent explanation — and the reviewer did exactly the
right thing with what was in front of them. Chasing "why did my run pass and theirs fail" as a test
bug, or worse "fixing" already-correct code to make the report go away, both lead somewhere wrong.

A related one landed the same hour: a `make ci` here reported 15 failures and 135 errors because the
Postgres container had been removed underneath it. Same family — shared mutable state between agents
— and the same tell, a failure count far outside anything the change could explain.

**Two rules, one for each side.**

*If you are rewriting:* a worktree with another agent in it is shared state. Do history rewrites
and multi-step file surgery somewhere private — a scratch clone, or a second worktree — and move
the finished ref across. If it has to happen in place, say so before you start and again when you
are done.

*If you are reporting a failure in someone else's tree:* name the SHA you are describing, and check
it with `git show <sha>:<path>` or a clean checkout before you send. "What is in the directory" and
"what is in the commit" are different questions, and one command settles which you are looking at.
Skip it and you hand somebody mid-rewrite a convincing bug report about a state that never existed
— they will either chase a phantom test bug or "fix" correct code, and both end somewhere worse
than where they started.

Both rules were available here and neither was followed. Check the count as well, before believing
any failure: one that no diff could explain is usually the ground moving, not the code.
