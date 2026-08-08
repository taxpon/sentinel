"""Tests for the remediation state machine.

`SPEC_TRANSITIONS` below is a hand transcription of the transition table in
`docs/04-state-machine.md`. It is deliberately not derived from `sentinel.pipeline.state`: the point
of these tests is that the implementation's matrix and the spec's matrix are the same set of edges,
which a test iterating the implementation's own table could never show. Every legal edge is asserted
from the transcription, and every pair *not* in the transcription is asserted to raise.
"""

import pytest

from sentinel.pipeline.state import (
    CYCLE_LIMIT_EXHAUSTED,
    DEFAULT_MAX_FIX_CYCLES,
    IllegalTransitionError,
    State,
    Trigger,
    automatic_trigger,
    is_legal,
    transition,
)

# Transcribed from docs/04-state-machine.md, "States" table.
TERMINAL = (State.MERGED, State.BLOCKED, State.FAILED)
NON_TERMINAL = (
    State.QUEUED,
    State.SESSION_CREATED,
    State.RUNNING,
    State.PR_OPENED,
    State.CI_RUNNING,
    State.CI_FAILED,
    State.CI_PASSED,
    State.IN_REVIEW,
    State.CHANGES_REQUESTED,
)

# Transcribed from docs/04-state-machine.md, "Transitions" table: (from, trigger, to).
SPEC_TRANSITIONS: list[tuple[State | None, Trigger, State]] = [
    (None, Trigger.ISSUE_LABELLED, State.QUEUED),
    (State.QUEUED, Trigger.SESSION_CREATED, State.SESSION_CREATED),
    (State.SESSION_CREATED, Trigger.SESSION_RUNNING, State.RUNNING),
    (State.RUNNING, Trigger.PR_OPENED, State.PR_OPENED),
    (State.PR_OPENED, Trigger.CHECK_SUITE_REQUESTED, State.CI_RUNNING),
    (State.RUNNING, Trigger.CHECK_SUITE_REQUESTED, State.CI_RUNNING),
    (State.CI_RUNNING, Trigger.CHECK_SUITE_SUCCEEDED, State.CI_PASSED),
    (State.RUNNING, Trigger.CHECK_SUITE_SUCCEEDED, State.CI_PASSED),
    (State.CI_RUNNING, Trigger.CHECK_SUITE_FAILED, State.CI_FAILED),
    (State.RUNNING, Trigger.CHECK_SUITE_FAILED, State.CI_FAILED),
    (State.CI_FAILED, Trigger.SESSION_RESUMED, State.RUNNING),
    (State.CI_PASSED, Trigger.REVIEW_REQUESTED, State.IN_REVIEW),
    (State.IN_REVIEW, Trigger.CHANGES_REQUESTED, State.CHANGES_REQUESTED),
    (State.CHANGES_REQUESTED, Trigger.SESSION_RESUMED, State.RUNNING),
    (State.IN_REVIEW, Trigger.PR_MERGED, State.MERGED),
    # "any -> BLOCKED" and "any -> FAILED", where "any" is every non-terminal state.
    *[(state, Trigger.BLOCKED, State.BLOCKED) for state in NON_TERMINAL],
    *[(state, Trigger.FAILED, State.FAILED) for state in NON_TERMINAL],
]

# The two loop edges, spelled out because the cycle rules hang off exactly these.
LOOP_EDGES = [
    (State.CI_FAILED, Trigger.SESSION_RESUMED),
    (State.CHANGES_REQUESTED, Trigger.SESSION_RESUMED),
]

SPEC_PAIRS = {(source, trigger) for source, trigger, _ in SPEC_TRANSITIONS}

# Every pair the spec does not list, terminal sources excluded — those absorb rather than raise.
ILLEGAL_PAIRS: list[tuple[State | None, Trigger]] = [
    (source, trigger)
    for source in (None, *NON_TERMINAL)
    for trigger in Trigger
    if (source, trigger) not in SPEC_PAIRS
]


def edge_id(edge: tuple[State | None, Trigger, State]) -> str:
    source, trigger, target = edge
    return f"{source or 'new'}-{trigger}-{target}"


def pair_id(pair: tuple[State | None, Trigger]) -> str:
    source, trigger = pair
    return f"{source or 'new'}-{trigger}"


@pytest.mark.parametrize("edge", SPEC_TRANSITIONS, ids=edge_id)
def test_every_legal_transition_reaches_the_spec_state(
    edge: tuple[State | None, Trigger, State],
) -> None:
    source, trigger, target = edge

    result = transition(source, trigger)

    assert result.from_state == source
    assert result.trigger is trigger
    assert result.to_state is target
    assert result.moved
    assert is_legal(source, trigger)


@pytest.mark.parametrize("pair", ILLEGAL_PAIRS, ids=pair_id)
def test_pairs_absent_from_the_spec_raise(pair: tuple[State | None, Trigger]) -> None:
    """The complement of the matrix: no edge exists that the spec does not list."""
    source, trigger = pair

    with pytest.raises(IllegalTransitionError) as excinfo:
        transition(source, trigger)

    assert not is_legal(source, trigger)
    message = str(excinfo.value)
    assert str(source or "(new)") in message
    assert str(excinfo.value.to_state) in message


@pytest.mark.parametrize("source", TERMINAL, ids=str)
@pytest.mark.parametrize("trigger", list(Trigger), ids=str)
def test_terminal_states_absorb_every_trigger(source: State, trigger: Trigger) -> None:
    result = transition(source, trigger, cycle=2)

    assert result.to_state is source
    assert result.cycle == 2
    assert result.absorbed
    assert not result.moved
    assert not is_legal(source, trigger)


@pytest.mark.parametrize("edge", SPEC_TRANSITIONS, ids=edge_id)
@pytest.mark.parametrize("cycle", [0, 1, 5])
def test_cycle_never_decreases_and_only_the_loop_edges_raise_it(
    edge: tuple[State | None, Trigger, State], cycle: int
) -> None:
    source, trigger, _ = edge

    result = transition(source, trigger, cycle=cycle, max_fix_cycles=99)

    assert result.cycle >= cycle
    expected = cycle + 1 if (source, trigger) in LOOP_EDGES else cycle
    assert result.cycle == expected


@pytest.mark.parametrize("source", [source for source, _ in LOOP_EDGES], ids=str)
@pytest.mark.parametrize("cycle", [0, 1, 2])
def test_a_resume_within_the_limit_increments_the_cycle(source: State, cycle: int) -> None:
    result = transition(source, Trigger.SESSION_RESUMED, cycle=cycle, max_fix_cycles=3)

    assert result.to_state is State.RUNNING
    assert result.cycle == cycle + 1
    assert result.reason is None


@pytest.mark.parametrize("source", [source for source, _ in LOOP_EDGES], ids=str)
def test_a_resume_past_the_limit_fails_instead_of_looping(source: State) -> None:
    result = transition(source, Trigger.SESSION_RESUMED, cycle=3, max_fix_cycles=3)

    assert result.to_state is State.FAILED
    assert result.cycle == 3
    assert result.reason == CYCLE_LIMIT_EXHAUSTED
    assert result.moved


def test_a_zero_limit_forbids_the_first_fix_cycle() -> None:
    result = transition(State.CI_FAILED, Trigger.SESSION_RESUMED, max_fix_cycles=0)

    assert result.to_state is State.FAILED
    assert result.cycle == 0


def test_the_review_fix_loop_runs_to_the_default_limit_then_fails() -> None:
    """Walk the loop end to end: three fixes are allowed, the fourth escalates."""
    state, cycle = State.QUEUED, 0
    for trigger in (Trigger.SESSION_CREATED, Trigger.SESSION_RUNNING, Trigger.PR_OPENED):
        result = transition(state, trigger, cycle=cycle)
        state, cycle = result.to_state, result.cycle

    for expected_cycle in range(1, DEFAULT_MAX_FIX_CYCLES + 1):
        for trigger in (Trigger.CHECK_SUITE_REQUESTED, Trigger.CHECK_SUITE_FAILED):
            result = transition(state, trigger, cycle=cycle)
            state, cycle = result.to_state, result.cycle
        assert state is State.CI_FAILED

        result = transition(state, Trigger.SESSION_RESUMED, cycle=cycle)
        state, cycle = result.to_state, result.cycle
        assert state is State.RUNNING
        assert cycle == expected_cycle

    result = transition(State.CI_FAILED, Trigger.SESSION_RESUMED, cycle=cycle)

    assert result.to_state is State.FAILED
    assert result.reason == CYCLE_LIMIT_EXHAUSTED


def test_the_happy_path_reaches_merged_without_a_fix_cycle() -> None:
    state, cycle = State.CI_RUNNING, 0
    for trigger in (
        Trigger.CHECK_SUITE_SUCCEEDED,
        Trigger.REVIEW_REQUESTED,
        Trigger.PR_MERGED,
    ):
        result = transition(state, trigger, cycle=cycle)
        state, cycle = result.to_state, result.cycle

    assert state is State.MERGED
    assert cycle == 0


@pytest.mark.parametrize("state", [*NON_TERMINAL, *TERMINAL], ids=str)
def test_only_ci_passed_carries_an_automatic_follow_up(state: State) -> None:
    expected = Trigger.REVIEW_REQUESTED if state is State.CI_PASSED else None

    assert automatic_trigger(state) == expected


def test_the_error_names_both_states_and_the_legal_sources() -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        transition(State.QUEUED, Trigger.PR_MERGED)

    error = excinfo.value
    assert error.from_state is State.QUEUED
    assert error.to_state is State.MERGED
    assert error.trigger is Trigger.PR_MERGED
    assert str(error) == (
        "illegal transition QUEUED -> MERGED on trigger pr_merged: "
        "MERGED is reachable only from IN_REVIEW"
    )
