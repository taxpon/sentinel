"""What the check runs on one head SHA say about the pull request.

A pure function over `sentinel.github.client.CheckRun`: no HTTP, no database, no clock. The worker
fetches, this decides, and `sentinel.pipeline.state` applies the trigger it names.

**Why this exists.** `docs/04-state-machine.md` used to read a `check_suite.completed` conclusion as
the CI signal, on the assumption that the fork's own scoped workflow would be the one talking. It is
not: GitHub raises one check suite per app and per workflow, and `taxpon/superset` carries 46 of
them. On the first live remediation that assumption produced both errors in one run — `CI_PASSED`
from a workflow that checks for a `hold` label, three seconds after the pull request opened, and
then `CI_FAILED` from `Dependency Review`, which fails on every pull request in that fork for a
reason no diff can address, spending a fix cycle on it
([ADR](../../../docs/adr/2026-08-10-ci-green-is-the-aggregate-of-the-check-runs.md)).

So a suite conclusion is a reason to *ask*, and the answer is read off every check run on the SHA at
once.

**The gate.** `devin-autofix-ci` is the `if: always()` conclusion job of
`docs/fork-ci/devin-autofix-ci.yml`, which already fails when any scoped signal did. Asking for that
one name is therefore the whole of "our own CI passed", and no list of job names has to be kept in
step with the workflow.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Final

from sentinel.github.client import FAILING_CONCLUSIONS, CheckRun
from sentinel.pipeline.state import Trigger

COMPLETED: Final = "completed"
"""`check_run.status` for a run that has reached a conclusion. Anything else — `queued`,
`in_progress`, `waiting`, `pending`, `requested` — is a check the SHA is still owed."""


class CIVerdict(StrEnum):
    """What the whole SHA says, and the trigger each answer implies.

    Deliberately three-valued. Two values would have to fold `PENDING` into one of the others, and
    both foldings are the defect this module exists to fix: into `GREEN` is the false green, into
    `FAILED` resumes a session over a check that has not finished.
    """

    PENDING = "pending"
    FAILED = "failed"
    GREEN = "green"

    @property
    def trigger(self) -> Trigger:
        """The trigger `docs/04-state-machine.md` applies for this verdict."""
        return _TRIGGERS[self]


_TRIGGERS: Final[dict[CIVerdict, Trigger]] = {
    CIVerdict.PENDING: Trigger.CHECK_SUITE_REQUESTED,
    CIVerdict.FAILED: Trigger.CHECK_SUITE_FAILED,
    CIVerdict.GREEN: Trigger.CHECK_SUITE_SUCCEEDED,
}


def verdict(runs: Iterable[CheckRun], *, gate: str) -> CIVerdict:
    """What `runs` — every check run on one head SHA — say about it.

    `gate` is the name of the check run that judges the diff, `CI_REQUIRED_CHECK_NAME`.

    In order:

    1. **The gate failed** → `FAILED`, whatever else is still running. A failure the session can act
       on is news immediately; making the loop wait for an unrelated Cypress shard to finish first
       is latency spent on nothing.
    2. **The gate has not concluded** → `PENDING`. This covers the gate being absent altogether,
       which is what the SHA looks like for the first minutes of every run and is the exact window
       the old rule turned into a false green.
    3. **Something else is still running** → `PENDING`.
    4. **Something else failed** → `PENDING`, not `FAILED`. See `foreign_failures`.
    5. Otherwise → `GREEN`.
    """
    by_name = {run.name: run for run in runs}
    gate_run = by_name.get(gate)

    if gate_run is not None and _failed(gate_run):
        return CIVerdict.FAILED
    if gate_run is None or gate_run.conclusion != "success":
        return CIVerdict.PENDING
    if any(run.status != COMPLETED for run in by_name.values()):
        return CIVerdict.PENDING
    if any(_failed(run) for run in by_name.values()):
        return CIVerdict.PENDING
    return CIVerdict.GREEN


def foreign_failures(runs: Iterable[CheckRun], *, gate: str) -> Sequence[str]:
    """The names of failing check runs that are not the gate, in the order GitHub returned them.

    These hold a pull request out of `GREEN` without ever producing `FAILED`, so the remediation
    waits in `CI_RUNNING` rather than resuming the session. That asymmetry is the point: a check
    outside `devin-autofix-ci` has not judged the diff, and the fork's `Dependency Review` fails on
    every pull request there for a repository-settings reason — resuming Devin over it burns the
    cycle budget on something no diff can change, which is what happened on the first live
    remediation.

    The cost is a remediation that waits indefinitely for a check that will never pass. That is
    recoverable and visible, where a spent cycle budget is neither, and it is what
    `docs/blockers.md#b2` requires the fork's inherited workflows to be disabled for. The worker
    logs this list so the wait is diagnosable rather than mysterious.
    """
    return [run.name for run in runs if run.name != gate and _failed(run)]


def _failed(run: CheckRun) -> bool:
    """Whether one check run reached a conclusion the pipeline treats as a failure.

    `FAILING_CONCLUSIONS` rather than a second vocabulary of this module's own: which conclusions
    mean failure is one fact, and two spellings of it would drift into disagreeing about a
    `startup_failure`. `cancelled`, `neutral` and `skipped` are absent from it and so pass as
    complete-and-not-failing — which is what makes a `skipped` job inside our own workflow, such as
    `pytest (scoped)` on a frontend-only diff, stop holding the pull request out of `GREEN`.
    """
    return run.status == COMPLETED and run.conclusion in FAILING_CONCLUSIONS
