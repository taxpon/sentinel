---
description: Review, record the review, and open the pull request for the current task
---

Take the current task from "code written" to "pull request open". Work through this in order — step
6 writes the marker that unblocks `gh pr create`, and the hook will refuse without it.

1. **Self-check against the definition of done.**
   - Tests exist for everything changed, and they assert behaviour rather than invocation.
   - `make lint && make test` are green. Fix failures; never weaken an assertion to get past one.
   - `git diff --name-only main...HEAD` contains **nothing outside the issue's owned files**. If it
     does, revert the stray change and raise it on the issue instead.
   - If the code diverged from the spec, `docs/` was updated in the same branch.

2. **UI tasks:** confirm component tests cover rendering, prop branches, empty and error states, and
   number formatting. Browser-level tests are out of scope; if a component test could not reach
   something, say which gap is left uncovered in the pull request body.

3. **ADRs.** Did you choose between defensible options, or do something a later reader would
   question? Write a record in `docs/adr/` using `template.md` — **and commit only that record.**
   `docs/adr/index.md` and `.claude/rules/adr-pointers.md` are regenerated on `main` after this
   merges, and a branch that edits them fails CI. `make adr-index` is still there if you want to
   read the index locally; revert it before committing:
   ```bash
   git checkout origin/main -- docs/adr/index.md .claude/rules/adr-pointers.md
   ```
   A record of `type: architecture` also needs a row in the **Design decisions** table of
   `docs/02-architecture.md`. That one you do commit.

4. **Catch up with `main`, if it has moved.** Do it now rather than after the review — the marker in
   step 6 covers one commit, so rebasing afterwards invalidates it.
   ```bash
   git fetch origin && git rebase origin/main
   ```
   One conflict is expected on a repository this parallel:
   - `docs/02-architecture.md` conflicts when another task also added a row to the **Design
     decisions** table. Keep **both** rows; the table has no meaningful order. This is a real edit by
     someone else — read their row before you drop anything.

   Anything else that conflicts is two tasks writing the same file, which the ownership rule says
   should not happen. Say so on the issue instead of resolving it quietly.

5. **Commit** everything, in English, then run the review:
   ```
   /pr-review-toolkit:review-pr
   ```
   Apply the findings. Re-run until nothing remains. If you disagree with a finding, say why in the
   pull request body rather than ignoring it silently.

6. **Record the review** against the exact commit it covered:
   ```bash
   mkdir -p .sentinel-review
   git rev-parse HEAD > ".sentinel-review/$(git branch --show-current | tr '/' '_').ok"
   ```
   Any commit after this invalidates the marker — if you change code, return to step 5.

7. **Open the pull request**, referencing the issue so it closes on merge:
   ```bash
   gh pr create --base main --title "<Tid>: <title>" --body "...

   Closes #<N>"
   ```
   The body states what changed, what is tested, and any decision recorded as an ADR.

8. **Report** the pull request URL, what the review found and how it was resolved, and anything left
   undone. If something was left out, say so plainly rather than letting the checklist imply it was
   finished.
