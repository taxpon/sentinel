"""What the check runs on one head SHA say — `sentinel.github.checks`.

The table below is the whole contract, and every row is a shape that actually occurred on
`taxpon/superset` PR #9, the first live remediation. That run is why this module exists: 27 check
suites on one SHA, of which the first to conclude was a workflow checking for a `hold` label, and
one of which fails on every pull request in that fork for a reason no diff can address.

These are pure-function tests. What the *pipeline* does with a verdict is `tests/test_worker.py`.
"""

from __future__ import annotations

import pytest

from sentinel.github.checks import CIVerdict, foreign_failures, verdict
from sentinel.github.client import CheckRun
from sentinel.pipeline.state import Trigger

GATE = "devin-autofix-ci"


def run(name: str, conclusion: str | None, *, status: str = "completed") -> CheckRun:
    return CheckRun(
        id=abs(hash((name, conclusion, status))),
        name=name,
        status=status,
        conclusion=conclusion,
        html_url=None,
        started_at=None,
        completed_at=None,
    )


def pending(name: str) -> CheckRun:
    return run(name, None, status="in_progress")


# The five jobs of `docs/fork-ci/devin-autofix-ci.yml`, as they concluded on PR #9. `pytest
# (scoped)` was skipped, correctly: the diff was two frontend files.
OUR_WORKFLOW = [
    run("Resolve scope from the diff", "success"),
    run("pre-commit (changed files)", "success"),
    run("jest (scoped)", "success"),
    run("pytest (scoped)", "skipped"),
    run(GATE, "success"),
]

# The inherited workflows that had concluded on PR #9 by the time Sentinel called it green.
TRIVIALLY_GREEN = [
    run("check-hold-label", "success"),
    run("labeler", "success"),
    run("unit-tests-required", "success"),
    run("unit-tests", "skipped"),
]


def test_the_first_trivial_success_is_not_green() -> None:
    """The defect, exactly. `check-hold-label` concluded 13 seconds after the pull request opened
    and moved the remediation to `CI_PASSED`, then straight on to `IN_REVIEW` — a review requested
    on the strength of a label check, three and a quarter minutes before the scoped suite that
    judges the diff had finished."""
    assert verdict(TRIVIALLY_GREEN, gate=GATE) is CIVerdict.PENDING


def test_an_absent_gate_is_pending_however_much_else_has_passed() -> None:
    assert verdict([*TRIVIALLY_GREEN, run("CodeQL", "success")], gate=GATE) is CIVerdict.PENDING


def test_the_whole_sha_green_is_green() -> None:
    assert verdict([*OUR_WORKFLOW, *TRIVIALLY_GREEN], gate=GATE) is CIVerdict.GREEN


def test_a_skipped_job_inside_our_own_workflow_does_not_hold_it_back() -> None:
    """`pytest (scoped)` is skipped on every frontend-only diff, and `jest (scoped)` on every
    backend-only one. Treating either as failure would mean no remediation is ever green."""
    assert verdict(OUR_WORKFLOW, gate=GATE) is CIVerdict.GREEN


def test_a_pending_check_holds_the_pull_request_out_of_green() -> None:
    """`cypress-matrix`, `playwright-tests` and `docker-build (dev)` were still running when
    Sentinel reported green, and `sharded-jest-tests` had eight shards outstanding."""
    runs = [*OUR_WORKFLOW, pending("cypress-matrix (chrome)")]

    assert verdict(runs, gate=GATE) is CIVerdict.PENDING


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure"])
def test_the_gate_failing_is_a_failure(conclusion: str) -> None:
    runs = [*OUR_WORKFLOW[:-1], run(GATE, conclusion)]

    assert verdict(runs, gate=GATE) is CIVerdict.FAILED


def test_the_gate_failing_beats_anything_still_running() -> None:
    """Reported without waiting for the rest of the SHA. The failure is news the session can act on
    now, and holding it until an unrelated Cypress shard finishes is latency spent on nothing."""
    runs = [*OUR_WORKFLOW[:-1], run(GATE, "failure"), pending("cypress-matrix (chrome)")]

    assert verdict(runs, gate=GATE) is CIVerdict.FAILED


def test_a_foreign_failure_is_not_a_failure() -> None:
    """`Dependency Review` on the fork: *"Dependency review is not supported on this repository"*.
    It fails on every pull request there and no diff can change it.

    Under the old rule this arrived as `check_suite.completed` with conclusion `failure`, moved the
    remediation `IN_REVIEW -> CI_FAILED`, and resumed the Devin session five seconds later — one of
    three fix cycles spent on a repository setting. It must never again produce `FAILED`.
    """
    runs = [*OUR_WORKFLOW, run("dependency-review", "failure")]

    assert verdict(runs, gate=GATE) is CIVerdict.PENDING


def test_a_foreign_failure_is_named_so_the_wait_is_diagnosable() -> None:
    """The cost of the rule above is a pull request that never reaches `CI_PASSED`. That is the
    price of not spending cycles on it, and `docs/blockers.md#b2` is what removes the cause — but a
    remediation waiting for a reason nobody can see is its own defect."""
    runs = [*OUR_WORKFLOW, run("dependency-review", "failure"), run("some-other-check", "failure")]

    assert foreign_failures(runs, gate=GATE) == ["dependency-review", "some-other-check"]


def test_the_gate_is_not_reported_as_a_foreign_failure() -> None:
    runs = [*OUR_WORKFLOW[:-1], run(GATE, "failure")]

    assert foreign_failures(runs, gate=GATE) == []


@pytest.mark.parametrize("conclusion", ["cancelled", "neutral", "skipped", "success"])
def test_no_other_foreign_conclusion_holds_the_pull_request_back(conclusion: str) -> None:
    runs = [*OUR_WORKFLOW, run("some-inherited-check", conclusion)]

    assert verdict(runs, gate=GATE) is CIVerdict.GREEN


def test_a_sha_with_no_checks_at_all_is_pending() -> None:
    """The first seconds after a push, before any workflow has been dispatched. Nothing is failing
    and nothing is incomplete, which is the exact shape an "all clear" rule reads as green."""
    assert verdict([], gate=GATE) is CIVerdict.PENDING


def test_the_gate_is_matched_by_name_from_the_configuration() -> None:
    renamed = [run("our-own-gate", "success")]

    assert verdict(renamed, gate="our-own-gate") is CIVerdict.GREEN
    assert verdict(renamed, gate=GATE) is CIVerdict.PENDING


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (CIVerdict.PENDING, Trigger.CHECK_SUITE_REQUESTED),
        (CIVerdict.FAILED, Trigger.CHECK_SUITE_FAILED),
        (CIVerdict.GREEN, Trigger.CHECK_SUITE_SUCCEEDED),
    ],
)
def test_every_verdict_names_the_trigger_it_implies(outcome: CIVerdict, expected: Trigger) -> None:
    # A verdict with no trigger would be a state the worker could reach and not act on.
    assert outcome.trigger is expected
