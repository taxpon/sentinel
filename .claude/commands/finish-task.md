---
description: Review, record the review, and open the pull request for the current task
---

Take the current task from "code written" to "pull request open". Work through this in order — step
5 writes the marker that unblocks `gh pr create`, and the hook will refuse without it.

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
   question? Write a record in `docs/adr/` using `template.md`, then:
   ```bash
   make adr-index
   ```
   Commit the regenerated `docs/adr/index.md` and `.claude/rules/adr-pointers.md`.

4. **Commit** everything, in English, then run the review:
   ```
   /pr-review-toolkit:review-pr
   ```
   Apply the findings. Re-run until nothing remains. If you disagree with a finding, say why in the
   pull request body rather than ignoring it silently.

5. **Record the review** against the exact commit it covered:
   ```bash
   mkdir -p .sentinel-review
   git rev-parse HEAD > ".sentinel-review/$(git branch --show-current | tr '/' '_').ok"
   ```
   Any commit after this invalidates the marker — if you change code, return to step 4.

6. **Open the pull request**, referencing the issue so it closes on merge:
   ```bash
   gh pr create --base main --title "<Tid>: <title>" --body "...

   Closes #<N>"
   ```
   The body states what changed, what is tested, and any decision recorded as an ADR.

7. **Report** the pull request URL, what the review found and how it was resolved, and anything left
   undone. If something was left out, say so plainly rather than letting the checklist imply it was
   finished.
