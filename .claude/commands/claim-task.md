---
description: Claim a Sentinel task issue and set up its worktree
argument-hint: "[issue number, or blank to pick the next ready task]"
---

Claim a task and prepare to work on it.

1. **Choose the task.**
   - If `$ARGUMENTS` names an issue number, use it.
   - Otherwise run `uv run scripts/session_context.py` and take the lowest-wave ready task. If
     several are ready, prefer one on the critical path in `docs/implementation-plan.md`.
   - Verify every dependency listed on the issue is **closed**. If not, stop and report which one
     blocks it — do not start work that will need rebasing onto unmerged code.

2. **Claim it**, so no other session picks it up:
   ```bash
   gh issue edit <N> -R taxpon/sentinel --add-assignee @me --add-label status:claimed
   ```

3. **Create the worktree** — never work on `main`, and never share a tree with another session:
   ```bash
   git worktree add ../wt/<Tid>-<slug> -b task/<Tid>-<slug>
   ```
   The branch name must match `task/T<id>-<slug>` or the session-start hook cannot identify the task.

4. **Load the working set**, and only this:
   - the issue body;
   - the one spec document named in its **Spec** field;
   - the decision records listed under its **Related ADRs**;
   - `docs/blockers.md` if the task touches Devin or the fork.

5. **Report before writing code:** the task, its owned files, the spec section you will implement,
   the decisions that constrain it, and anything in the issue that looks wrong or underspecified.
   Raise the concern on the issue rather than silently reinterpreting the task.
