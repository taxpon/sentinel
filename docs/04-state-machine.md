# Remediation lifecycle

> **Status:** Design · **Answers:** What states can a remediation be in, what moves it between them, and how does the review-fix loop work?

## States

```mermaid
stateDiagram-v2
    [*] --> QUEUED
    QUEUED --> SESSION_CREATED
    SESSION_CREATED --> RUNNING
    RUNNING --> PR_OPENED
    PR_OPENED --> CI_RUNNING
    RUNNING --> CI_RUNNING : fix pushed
    CI_RUNNING --> CI_FAILED
    CI_RUNNING --> CI_PASSED
    RUNNING --> CI_FAILED : requested missed
    RUNNING --> CI_PASSED : requested missed
    PR_OPENED --> CI_FAILED : requested missed
    PR_OPENED --> CI_PASSED : requested missed
    CI_FAILED --> CI_PASSED : suite re-run
    CI_PASSED --> CI_FAILED : later suite
    IN_REVIEW --> CI_FAILED : later suite
    CI_FAILED --> RUNNING : resume, cycle + 1
    CI_PASSED --> IN_REVIEW
    IN_REVIEW --> CHANGES_REQUESTED
    CHANGES_REQUESTED --> RUNNING : resume, cycle + 1
    IN_REVIEW --> MERGED
    QUEUED --> BLOCKED
    RUNNING --> BLOCKED
    CI_FAILED --> FAILED
    CHANGES_REQUESTED --> FAILED
    RUNNING --> FAILED
    MERGED --> [*]
    BLOCKED --> [*]
    FAILED --> [*]
```

| State | Meaning |
|---|---|
| `QUEUED` | Issue accepted, job enqueued, no Devin session yet |
| `SESSION_CREATED` | Devin session exists; work has not been observed starting |
| `RUNNING` | Devin is working (session status `running`/`claimed`/`resuming`) |
| `PR_OPENED` | A pull request attributable to this remediation exists |
| `CI_RUNNING` | A check suite is in progress on the head SHA |
| `CI_FAILED` | The check suite failed — the loop trigger |
| `CI_PASSED` | All required checks green |
| `IN_REVIEW` | Awaiting human review |
| `CHANGES_REQUESTED` | A reviewer requested changes — the second loop trigger |
| `MERGED` | **Terminal, success.** |
| `BLOCKED` | **Terminal.** Devin reported it cannot proceed, or a policy limit stopped us. Escalated to a human. |
| `FAILED` | **Terminal.** The session errored, spent its ACU cap, or finished without a pull request; or the cycle limit was exhausted. |

## Transitions

| From | To | Trigger | Side effects |
|---|---|---|---|
| — | `QUEUED` | `issues.labeled` with `devin:autofix` | Create `remediation`, enqueue `create_session` |
| `QUEUED` | `SESSION_CREATED` | Worker created the session | `POST /v3/…/sessions`; store `devin_session_id`, `session_created_at` |
| `QUEUED` | `BLOCKED` | Daily ACU budget exhausted, or issue class unrecognised | Comment on issue, add `needs-human` |
| `SESSION_CREATED` | `RUNNING` | Poller sees any status but `new` — the session has been claimed. A session first observed at `exit` still passes through here, because invariant 5 puts `PR_OPENED` on every path to CI and `RUNNING` is the only state it is reachable from | — |
| `RUNNING` | `PR_OPENED` | `pull_requests[]` on the session, observed by the poller — **only while no pull request is linked**. The `pull_request.opened` webhook carries no key that can find its remediation and is dropped ([ADR](./adr/2026-08-08-the-poller-links-the-pull-request.md)) | Link PR to remediation, set `pr_opened_at`. Write-once: a later observation is recorded and otherwise ignored ([ADR](./adr/2026-08-08-the-poller-records-only-what-moves.md)) |
| `PR_OPENED`, `RUNNING` | `CI_RUNNING` | `check_suite.requested` on the head SHA — **requires a linked pull request** | — |
| `PR_OPENED`, `RUNNING`, `CI_RUNNING`, `CI_FAILED` | `CI_PASSED` | `check_suite.completed`, conclusion `success` — **requires a linked pull request** | Set `ci_green_at` |
| `PR_OPENED`, `RUNNING`, `CI_RUNNING`, `CI_PASSED`, `IN_REVIEW` | `CI_FAILED` | `check_suite.completed`, conclusion `failure`/`timed_out` — **requires a linked pull request** | Enqueue `resume_session` |
| **`CI_FAILED`** | **`RUNNING`** | Worker resumed the session | Fetch failing job logs, `POST /v3/…/messages`, increment `cycle`, append tag `cycle:N` |
| `CI_PASSED` | `IN_REVIEW` | Entering `CI_PASSED` | Request review; post the structured-output summary as a PR comment |
| `IN_REVIEW` | `CHANGES_REQUESTED` | `pull_request_review.submitted`, state `changes_requested` | Enqueue `resume_session` |
| **`CHANGES_REQUESTED`** | **`RUNNING`** | Worker resumed the session | Forward review body and inline comments via `POST /v3/…/messages`, increment `cycle` |
| `IN_REVIEW` | `MERGED` | `pull_request.closed` with `merged: true` | Set `merged_at`, append tag `outcome:merged`, close the issue |
| any | `BLOCKED` | `structured_output.outcome == "blocked"`, or a session Devin reports as `running` and stalled on `status_detail: waiting_for_user` ([ADR](./adr/2026-08-08-a-stalled-session-is-blocked.md)) | Store `blocked_reason`, comment on issue, add `needs-human` |
| any | `FAILED` | Session status `error`; `cycle > MAX_FIX_CYCLES`; or, **while no pull request is linked**, `acus_consumed` reaching `ACU_CAPS[issue_class]` or Devin finishing the session with nothing to show ([ADR](./adr/2026-08-08-a-session-with-nothing-to-show-fails.md)) | Store reason, comment on issue, add `needs-human` |

## Check suite events

Sentinel does not choose when a check suite reports, so a `check_suite` event can arrive in **any**
state that can hold a linked pull request: `PR_OPENED`, `RUNNING`, `CI_RUNNING`, `CI_FAILED`,
`CI_PASSED`, `IN_REVIEW`, `CHANGES_REQUESTED`. None of them may reject one. Where the event carries
no verdict the state does not already have, it is recorded in `remediation_event` and otherwise
ignored — the same treatment a terminal state gives a late webhook.

| Trigger | Moves from | Recorded and ignored from |
|---|---|---|
| `check_suite.requested` | `PR_OPENED`, `RUNNING` | `CI_RUNNING`, `CI_FAILED`, `CI_PASSED`, `IN_REVIEW`, `CHANGES_REQUESTED` |
| `check_suite.completed` conclusion `success` | `PR_OPENED`, `RUNNING`, `CI_RUNNING`, `CI_FAILED` | `CI_PASSED`, `IN_REVIEW`, `CHANGES_REQUESTED` |
| `check_suite.completed` conclusion `failure` | `PR_OPENED`, `RUNNING`, `CI_RUNNING`, `CI_PASSED`, `IN_REVIEW` | `CI_FAILED`, `CHANGES_REQUESTED` |

Reading the rows:

- **`PR_OPENED` moves on a conclusion.** This is the ordinary first lap, not an edge case. The
  poller is what links the pull request ([ADR](./adr/2026-08-07-poller-drives-state-machine.md)),
  and it does so up to `POLL_INTERVAL_SECONDS` after the pull request appeared — routinely after
  the first `check_suite.requested` has already been dropped as unresolvable. The remediation is
  therefore sitting in `PR_OPENED`, not `CI_RUNNING`, when the conclusion arrives.
- **A suite starting again is never news.** Pulling a remediation out of review, or out of the fix
  loop, to record that CI restarted would lose the state that matters. The conclusion follows.
- **A second success is ignored.** `ci_green_at` is the *first* successful suite
  ([03](./03-data-model.md)), and a success must not drag a remediation back out of review.
- **A failure is news almost everywhere**, including `IN_REVIEW`, where nothing else would re-engage
  the fix loop. Not in `CHANGES_REQUESTED`, which already has a `resume_session` pending that will
  produce a fresh suite of its own, and not twice over in `CI_FAILED`.

## The review-fix loop

The two loop edges — `CI_FAILED → RUNNING` and `CHANGES_REQUESTED → RUNNING` — are the substance of
the system. They reuse the **existing** session (`resumable: true`) so Devin retains the context of
its own change instead of rediscovering it.

```mermaid
sequenceDiagram
    autonumber
    participant GH as GitHub
    participant API as api
    participant W as worker
    participant D as Devin session

    GH->>API: check_suite.completed — failure
    API->>API: resolve head SHA to remediation
    alt cycle >= MAX_FIX_CYCLES
        API->>GH: comment + needs-human label
        Note over API: state = FAILED
    else
        API->>W: enqueue resume_session
        W->>GH: fetch failing jobs and log excerpt
        W->>D: POST /v3/.../sessions/{id}/messages
        Note right of W: "CI failed on <sha>.<br/>Failing job: <name>.<br/>Excerpt: <log>.<br/>Diagnose and push a fix."
        W->>D: POST /v3/.../sessions/{id}/tags — cycle:N
        Note over W: state = RUNNING, cycle + 1
        D->>GH: push fix commit
        GH->>API: check_suite.completed — success
        Note over API: state = CI_PASSED
    end
```

The CI states are re-entered from `RUNNING` on the second and later passes round the loop: the pull
request already exists, so the check suite for the fix commit is observed while the session is
running. That is one case of the general rule above — a conclusion is accepted wherever a linked
pull request can sit.

The widening is bounded by the pull request itself, not by the lap count, because both halves of
the condition have to hold. A check suite is only meaningful once a pull request is linked, so the
CI triggers require one — which keeps `PR_OPENED` on every path into the CI states rather than
letting `RUNNING → CI_PASSED` bypass it. And `RUNNING → PR_OPENED` is only legal while none is
linked, so the poller, which re-reads `pull_requests[]` on every poll
([ADR](./adr/2026-08-07-poller-drives-state-machine.md)), cannot walk the state backwards out of the
loop or re-stamp `pr_opened_at` on lap two. A repeat observation is absorbed rather than rejected,
exactly as for a terminal state — but the poller does not record it: a webhook arrives once per real
event, while the poller re-reads the same session every `POLL_INTERVAL_SECONDS`, so only the
observations that move a remediation are written to `remediation_event`
([ADR](./adr/2026-08-08-the-poller-records-only-what-moves.md)).

The message states the failure and the goal; it does not prescribe the fix. Steering Devin
line-by-line would defeat the purpose and is explicitly avoided ([05](./05-devin-integration.md)).

## Invariants

1. **Terminal states are absorbing.** No transition leaves `MERGED`, `BLOCKED` or `FAILED`. A
   webhook arriving after a terminal state is recorded in `remediation_event` and otherwise ignored.
2. **One session per remediation.** Guaranteed by `UNIQUE (repo, issue_number)` plus
   `INSERT … ON CONFLICT DO NOTHING` ([03](./03-data-model.md)).
3. **`cycle` only increases,** and only on a transition into `RUNNING` from `CI_FAILED` or
   `CHANGES_REQUESTED`. `cycle > MAX_FIX_CYCLES` forces `FAILED`.
4. **Every transition writes exactly one `remediation_event`,** in the same transaction as the
   state column update. The log can never disagree with the column.
5. **A pull request is linked exactly once, and `PR_OPENED` is on every path to CI.** `pr_number`,
   `pr_url` and `pr_opened_at` are written when `PR_OPENED` is entered and never again. No sequence
   of triggers reaches `CI_RUNNING`, `CI_PASSED`, `CI_FAILED`, `IN_REVIEW`, `CHANGES_REQUESTED` or
   `MERGED` without passing through it. The funnel, the merge rate `merged / pr_opened` and
   time-to-PR in [07](./07-observability.md) all rest on this.
6. **Illegal transitions raise** rather than silently no-op, and are asserted in the test matrix
   ([08](./08-testing.md)).

## Escalation

`BLOCKED` and `FAILED` both escalate rather than terminate quietly:

- a comment on the originating issue containing Devin's `blocked_reason` or the policy reason, plus
  a link to the session;
- the `needs-human` label;
- the remediation stays visible on the dashboard's failure-breakdown panel
  ([07](./07-observability.md)).

Escalated work is **not** deleted or retried automatically. An unresolved item is a real signal
about the system's limits and is reported as such.
