"""Tests for the remediation state machine.

`SPEC_TRANSITIONS` below is a hand transcription of the transition table in
`docs/04-state-machine.md`. It is deliberately not derived from `sentinel.pipeline.state`: the point
of these tests is that the implementation's matrix and the spec's matrix are the same set of edges,
which a test iterating the implementation's own table could never show. Every legal edge is asserted
from the transcription, and every pair *not* in the transcription is asserted to raise.

The transcription is in turn pinned to the document it was transcribed from — see the tests at the
end, which parse `docs/04-state-machine.md` back out so that editing the spec without editing this
file fails, in the same spirit as `make adr-check`. That covers the state list, the From and To
columns, the two pull-request conditions, the state diagram, and the default cycle limit in
`docs/09-operations.md`. It does **not** cover the transition table's Trigger column, which is free
prose: which trigger fires which row is a human transcription, and rewriting that prose leaves this
suite green.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from sentinel.pipeline.state import (
    CYCLE_LIMIT_EXHAUSTED,
    DEFAULT_MAX_FIX_CYCLES,
    RULES,
    IllegalTransitionError,
    PullRequestCondition,
    State,
    Trigger,
    automatic_trigger,
    is_legal,
    transition,
)

SPEC_DOC = Path(__file__).resolve().parent.parent / "docs" / "04-state-machine.md"

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
    (State.PR_OPENED, Trigger.CHECK_SUITE_SUCCEEDED, State.CI_PASSED),
    (State.RUNNING, Trigger.CHECK_SUITE_SUCCEEDED, State.CI_PASSED),
    (State.CI_RUNNING, Trigger.CHECK_SUITE_SUCCEEDED, State.CI_PASSED),
    (State.CI_FAILED, Trigger.CHECK_SUITE_SUCCEEDED, State.CI_PASSED),
    (State.PR_OPENED, Trigger.CHECK_SUITE_FAILED, State.CI_FAILED),
    (State.RUNNING, Trigger.CHECK_SUITE_FAILED, State.CI_FAILED),
    (State.CI_RUNNING, Trigger.CHECK_SUITE_FAILED, State.CI_FAILED),
    (State.CI_PASSED, Trigger.CHECK_SUITE_FAILED, State.CI_FAILED),
    (State.IN_REVIEW, Trigger.CHECK_SUITE_FAILED, State.CI_FAILED),
    (State.CI_FAILED, Trigger.SESSION_RESUMED, State.RUNNING),
    (State.CI_PASSED, Trigger.REVIEW_REQUESTED, State.IN_REVIEW),
    (State.IN_REVIEW, Trigger.CHANGES_REQUESTED, State.CHANGES_REQUESTED),
    (State.CHANGES_REQUESTED, Trigger.SESSION_RESUMED, State.RUNNING),
    (State.IN_REVIEW, Trigger.PR_MERGED, State.MERGED),
    # "any -> BLOCKED" and "any -> FAILED", where "any" is every non-terminal state.
    *[(state, Trigger.BLOCKED, State.BLOCKED) for state in NON_TERMINAL],
    *[(state, Trigger.FAILED, State.FAILED) for state in NON_TERMINAL],
]

# Transcribed from the conditions in the same table's "Trigger" column.
NEEDS_A_LINKED_PR = frozenset(
    {Trigger.CHECK_SUITE_REQUESTED, Trigger.CHECK_SUITE_SUCCEEDED, Trigger.CHECK_SUITE_FAILED}
)
LINKS_THE_PR = frozenset({Trigger.PR_OPENED})

# Transcribed from the "Check suite events" table: where each trigger is legal but does not move.
SPEC_ABSORBED: dict[Trigger, frozenset[State]] = {
    Trigger.CHECK_SUITE_REQUESTED: frozenset(
        {
            State.CI_RUNNING,
            State.CI_FAILED,
            State.CI_PASSED,
            State.IN_REVIEW,
            State.CHANGES_REQUESTED,
        }
    ),
    Trigger.CHECK_SUITE_SUCCEEDED: frozenset(
        {State.CI_PASSED, State.IN_REVIEW, State.CHANGES_REQUESTED}
    ),
    Trigger.CHECK_SUITE_FAILED: frozenset({State.CI_FAILED, State.CHANGES_REQUESTED}),
}

# Transcribed from the same section's prose: every state that can hold a linked pull request.
SPEC_LINKED_STATES = frozenset(
    {
        State.PR_OPENED,
        State.RUNNING,
        State.CI_RUNNING,
        State.CI_FAILED,
        State.CI_PASSED,
        State.IN_REVIEW,
        State.CHANGES_REQUESTED,
    }
)

# Transcribed from invariant 5: the states no path may reach without passing through PR_OPENED.
POST_PR_STATES = frozenset(
    {
        State.CI_RUNNING,
        State.CI_PASSED,
        State.CI_FAILED,
        State.IN_REVIEW,
        State.CHANGES_REQUESTED,
        State.MERGED,
    }
)

# The two loop edges, spelled out because the cycle rules hang off exactly these.
LOOP_EDGES = [
    (State.CI_FAILED, Trigger.SESSION_RESUMED),
    (State.CHANGES_REQUESTED, Trigger.SESSION_RESUMED),
]

SPEC_PAIRS = {(source, trigger) for source, trigger, _ in SPEC_TRANSITIONS}
ABSORBED_PAIRS = {
    (source, trigger) for trigger, sources in SPEC_ABSORBED.items() for source in sources
}

# Every pair the spec does not list. Terminal sources and the absorbed pairs are excluded: both are
# legal and recorded, they just do not move, so they are covered by their own tests.
ILLEGAL_PAIRS: list[tuple[State | None, Trigger]] = [
    (source, trigger)
    for source in (None, *NON_TERMINAL)
    for trigger in Trigger
    if (source, trigger) not in SPEC_PAIRS and (source, trigger) not in ABSORBED_PAIRS
]


def pr_linked_for(trigger: Trigger) -> bool:
    """The linkage a trigger has to be applied under for its own condition to be satisfied."""
    return trigger in NEEDS_A_LINKED_PR


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
    linked = pr_linked_for(trigger)

    result = transition(source, trigger, pr_linked=linked)

    assert result.from_state == source
    assert result.trigger is trigger
    assert result.to_state is target
    assert result.moved
    assert is_legal(source, trigger, pr_linked=linked)


@pytest.mark.parametrize("pair", ILLEGAL_PAIRS, ids=pair_id)
def test_pairs_absent_from_the_spec_raise(pair: tuple[State | None, Trigger]) -> None:
    """The complement of the matrix: no edge exists that the spec does not list.

    Each pair is applied under the linkage its own trigger wants, so what is being asserted is the
    source restriction and not the pull-request condition.
    """
    source, trigger = pair
    linked = pr_linked_for(trigger)

    with pytest.raises(IllegalTransitionError) as excinfo:
        transition(source, trigger, pr_linked=linked)

    assert not is_legal(source, trigger, pr_linked=linked)
    message = str(excinfo.value)
    assert str(source or "(new)") in message
    assert str(excinfo.value.to_state) in message


@pytest.mark.parametrize("source", TERMINAL, ids=str)
@pytest.mark.parametrize("trigger", list(Trigger), ids=str)
def test_terminal_states_absorb_every_trigger(source: State, trigger: Trigger) -> None:
    result = transition(source, trigger, cycle=2, pr_linked=True)

    assert result.to_state is source
    assert result.cycle == 2
    assert result.absorbed
    assert not result.moved
    assert not is_legal(source, trigger, pr_linked=True)


# --------------------------------------------------------------------------- the pull request link


@pytest.mark.parametrize("source", [*NON_TERMINAL, *TERMINAL], ids=str)
def test_a_second_pull_request_observation_is_absorbed_from_any_state(source: State) -> None:
    """The poller re-reads `pull_requests[]` on every poll, from whatever state it finds."""
    result = transition(source, Trigger.PR_OPENED, cycle=2, pr_linked=True)

    assert result.to_state is source
    assert result.cycle == 2
    assert result.absorbed
    assert not is_legal(source, Trigger.PR_OPENED, pr_linked=True)


@pytest.mark.parametrize("trigger", sorted(NEEDS_A_LINKED_PR), ids=str)
def test_ci_triggers_need_a_linked_pull_request(trigger: Trigger) -> None:
    sources = [source for source, spec_trigger in SPEC_PAIRS if spec_trigger is trigger]
    assert sources, "the transcription lists at least one source for every CI trigger"

    for source in sources:
        with pytest.raises(IllegalTransitionError) as excinfo:
            transition(source, trigger, pr_linked=False)

        assert "needs a pull request linked" in str(excinfo.value)
        assert not is_legal(source, trigger, pr_linked=False)


def reachable_nodes() -> set[tuple[State | None, bool]]:
    """Every `(state, pr_linked)` a sequence of triggers can produce, from nothing.

    `cycle` is held at zero under a limit that cannot be hit, which does not narrow the search: the
    cycle affects the *outcome* of a resume, never its legality, and the `FAILED` it can force is
    already reachable.
    """
    start: tuple[State | None, bool] = (None, False)
    seen = {start}
    frontier = [start]
    while frontier:
        state, linked = frontier.pop()
        for trigger in Trigger:
            try:
                result = transition(state, trigger, pr_linked=linked, max_fix_cycles=99)
            except IllegalTransitionError:
                continue
            if not result.moved:
                continue
            node = (result.to_state, linked or result.to_state is State.PR_OPENED)
            if node not in seen:
                seen.add(node)
                frontier.append(node)
    return seen


def test_no_reachable_path_skips_pr_opened() -> None:
    """Invariant 5, by exhaustive search rather than by inspection."""
    seen = reachable_nodes()

    unlinked = {state for state, linked in seen if not linked}
    assert unlinked == {None, State.QUEUED, State.SESSION_CREATED, State.RUNNING, *TERMINAL[1:]}
    assert not unlinked & POST_PR_STATES
    assert (State.PR_OPENED, True) in seen
    assert (State.MERGED, True) in seen


# ------------------------------------------------------------------------------------ check suites


def test_no_linked_state_rejects_a_check_suite_event() -> None:
    """Sentinel does not choose when a suite reports, so no state it can reach may refuse one.

    Stated over the reachable graph rather than over the current table: enumerating the sources
    `RULES` happens to list would have passed while lap one was broken, because `PR_OPENED` was
    reachable and unlisted.
    """
    linked = {state for state, pr_linked in reachable_nodes() if pr_linked and state is not None}

    assert linked - set(TERMINAL) == SPEC_LINKED_STATES

    for state in sorted(linked):
        for trigger in sorted(NEEDS_A_LINKED_PR):
            result = transition(state, trigger, pr_linked=True, max_fix_cycles=99)
            assert result.to_state in {state, RULES[trigger].target}


@pytest.mark.parametrize("state", sorted(SPEC_LINKED_STATES), ids=str)
def test_a_ci_failure_is_never_silently_dropped(state: State) -> None:
    """From every linked state a failure either engages the fix loop or is already engaged."""
    result = transition(state, Trigger.CHECK_SUITE_FAILED, pr_linked=True)

    if result.moved:
        assert result.to_state is State.CI_FAILED
    else:
        assert state in {State.CI_FAILED, State.CHANGES_REQUESTED}


@pytest.mark.parametrize(
    "pair", sorted(ABSORBED_PAIRS, key=str), ids=lambda pair: f"{pair[0]}-{pair[1]}"
)
def test_a_check_suite_event_with_no_new_verdict_is_recorded_without_moving(
    pair: tuple[State, Trigger],
) -> None:
    source, trigger = pair

    result = transition(source, trigger, cycle=2, pr_linked=True)

    assert result.to_state is source
    assert result.cycle == 2
    assert result.absorbed
    assert not is_legal(source, trigger, pr_linked=True)


def test_the_transcribed_absorbing_states_match_the_rules() -> None:
    for trigger, sources in SPEC_ABSORBED.items():
        assert RULES[trigger].absorbed_from == sources
        assert sources <= RULES[trigger].sources, "an absorbing state that is not even a source"

    assert {trigger for trigger, rule in RULES.items() if rule.absorbed_from} == set(SPEC_ABSORBED)


def test_the_lap_one_ordering_completes_now_that_the_poller_links_the_pull_request() -> None:
    """`requested` is dropped as unresolvable, the poller links, then the conclusion lands.

    This is the ordinary first lap: the poll interval is wider than the gap between a pull request
    opening and its first check suite, so the remediation is in `PR_OPENED` — not `CI_RUNNING` —
    when the conclusion arrives.
    """
    walk = Walk()
    for trigger in (Trigger.ISSUE_LABELLED, Trigger.SESSION_CREATED, Trigger.SESSION_RUNNING):
        walk.apply(trigger)

    # check_suite.requested fires here and resolves to no remediation, so nothing is applied.
    walk.apply(Trigger.PR_OPENED)
    assert (walk.state, walk.pr_linked) == (State.PR_OPENED, True)

    walk.apply(Trigger.CHECK_SUITE_SUCCEEDED)
    assert walk.state is State.CI_PASSED

    follow_up = automatic_trigger(walk.state)
    assert follow_up is Trigger.REVIEW_REQUESTED
    walk.apply(follow_up)

    assert walk.state is State.IN_REVIEW


# --------------------------------------------------------------------------------- the cycle count


@pytest.mark.parametrize("edge", SPEC_TRANSITIONS, ids=edge_id)
@pytest.mark.parametrize("cycle", [0, 1, 5])
def test_cycle_never_decreases_and_only_the_loop_edges_raise_it(
    edge: tuple[State | None, Trigger, State], cycle: int
) -> None:
    source, trigger, _ = edge

    result = transition(
        source, trigger, cycle=cycle, pr_linked=pr_linked_for(trigger), max_fix_cycles=99
    )

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


# ------------------------------------------------------------------------------------ full journeys


class Walk:
    """A remediation walked trigger by trigger, tracking everything `transition` needs back."""

    def __init__(self) -> None:
        self.state: State | None = None
        self.cycle = 0
        self.pr_linked = False

    def apply(self, trigger: Trigger) -> None:
        result = transition(self.state, trigger, cycle=self.cycle, pr_linked=self.pr_linked)
        self.state, self.cycle = result.to_state, result.cycle
        if result.moved and result.to_state is State.PR_OPENED:
            self.pr_linked = True


def test_the_review_fix_loop_runs_to_the_default_limit_then_fails() -> None:
    """Walk the loop end to end: three fixes are allowed, the fourth escalates."""
    walk = Walk()
    for trigger in (
        Trigger.ISSUE_LABELLED,
        Trigger.SESSION_CREATED,
        Trigger.SESSION_RUNNING,
        Trigger.PR_OPENED,
    ):
        walk.apply(trigger)
    assert walk.pr_linked

    for expected_cycle in range(1, DEFAULT_MAX_FIX_CYCLES + 1):
        walk.apply(Trigger.CHECK_SUITE_REQUESTED)
        walk.apply(Trigger.CHECK_SUITE_FAILED)
        assert walk.state is State.CI_FAILED

        walk.apply(Trigger.SESSION_RESUMED)
        assert walk.state is State.RUNNING
        assert walk.cycle == expected_cycle

    walk.apply(Trigger.CHECK_SUITE_REQUESTED)
    walk.apply(Trigger.CHECK_SUITE_FAILED)
    walk.apply(Trigger.SESSION_RESUMED)

    assert walk.state is State.FAILED
    assert walk.cycle == DEFAULT_MAX_FIX_CYCLES


def test_the_happy_path_reaches_merged_through_pr_opened() -> None:
    walk = Walk()
    for trigger in (
        Trigger.ISSUE_LABELLED,
        Trigger.SESSION_CREATED,
        Trigger.SESSION_RUNNING,
        Trigger.PR_OPENED,
        Trigger.CHECK_SUITE_REQUESTED,
        Trigger.CHECK_SUITE_SUCCEEDED,
        Trigger.REVIEW_REQUESTED,
        Trigger.PR_MERGED,
    ):
        walk.apply(trigger)

    assert walk.state is State.MERGED
    assert walk.cycle == 0


def test_the_poller_may_re_observe_the_pull_request_at_any_point_of_a_loop() -> None:
    """The lap-two hazard: a re-fired PR_OPENED must not walk the state back out of the loop."""
    walk = Walk()
    for trigger in (
        Trigger.ISSUE_LABELLED,
        Trigger.SESSION_CREATED,
        Trigger.SESSION_RUNNING,
        Trigger.PR_OPENED,
        Trigger.CHECK_SUITE_REQUESTED,
        Trigger.CHECK_SUITE_FAILED,
        Trigger.SESSION_RESUMED,
    ):
        walk.apply(trigger)
    assert (walk.state, walk.cycle) == (State.RUNNING, 1)

    walk.apply(Trigger.PR_OPENED)

    assert (walk.state, walk.cycle) == (State.RUNNING, 1)


# ------------------------------------------------------------------------------------ the API shape


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


# ---------------------------------------------------------------- the transcription against the doc


def spec_table(section: str, doc: Path = SPEC_DOC) -> Iterator[list[str]]:
    """The rows of the one markdown table under `## <section>`, header and rule dropped."""
    lines = doc.read_text().splitlines()
    body = lines[lines.index(f"## {section}") + 1 :]
    rows = []
    for line in body:
        if line.startswith("## "):
            break
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not all(set(cell) <= set("-: ") for cell in cells):
                rows.append(cells)
    assert len(rows) > 1, f"no table found under '## {section}' in {doc.name}"
    return iter(rows[1:])


def names_in(cell: str) -> list[str]:
    return [part.strip().strip("*`") for part in cell.split(",")]


def spec_diagram_edges() -> set[tuple[str | None, str]]:
    """The `A --> B` edges of the state diagram, with the initial `[*]` read as `None`.

    Terminal `X --> [*]` markers are dropped: they say a state is final, which the "States" table
    already says and the transition table has no row for.
    """
    lines = SPEC_DOC.read_text().splitlines()
    body = lines[lines.index("```mermaid") + 1 :]
    edges = set()
    for line in body:
        if line.startswith("```"):
            break
        if "-->" not in line:
            continue
        source, _, rest = line.partition("-->")
        target = rest.split(":")[0].strip()
        if target == "[*]":
            continue
        edges.add((None if source.strip() == "[*]" else source.strip(), target))
    return edges


def test_the_transcribed_states_match_the_spec_document() -> None:
    rows = list(spec_table("States"))

    assert {names_in(row[0])[0] for row in rows} == {state.value for state in State}
    terminal = {names_in(row[0])[0] for row in rows if row[1].startswith("**Terminal")}
    assert terminal == {state.value for state in TERMINAL}


def test_the_transcribed_transitions_match_the_spec_document() -> None:
    documented: set[tuple[str | None, str]] = set()
    for row in spec_table("Transitions"):
        target = names_in(row[1])[0]
        for source in names_in(row[0]):
            if source == "—":
                documented.add((None, target))
            elif source == "any":
                documented.update((state.value, target) for state in NON_TERMINAL)
            else:
                documented.add((source, target))

    transcribed = {
        (source.value if source else None, target.value) for source, _, target in SPEC_TRANSITIONS
    }
    assert documented == transcribed


def test_the_transcribed_check_suite_table_matches_the_spec_document() -> None:
    """Both columns, so the document and `RULES` cannot drift on which events move and which do not.

    Only the trigger each row names is transcribed here; the rest is read off the document.
    """
    moves: dict[Trigger, set[str]] = {}
    ignored: dict[Trigger, set[str]] = {}
    for row in spec_table("Check suite events"):
        # The row names its own trigger. This used to be inferred from words like "success" in the
        # prose, which silently collapsed two rows onto one trigger the moment the document stopped
        # describing check suite conclusions and started describing verdicts.
        [trigger] = [
            candidate for candidate in NEEDS_A_LINKED_PR if f"`{candidate.value}`" in row[0]
        ]
        moves[trigger] = set(names_in(row[1]))
        ignored[trigger] = set(names_in(row[2]))

    assert set(moves) == NEEDS_A_LINKED_PR
    for trigger in NEEDS_A_LINKED_PR:
        transcribed_moves = {
            source.value
            for source, spec_trigger, _ in SPEC_TRANSITIONS
            if spec_trigger is trigger and source is not None
        }
        assert moves[trigger] == transcribed_moves
        assert ignored[trigger] == {state.value for state in SPEC_ABSORBED[trigger]}
        assert moves[trigger] | ignored[trigger] == {state.value for state in SPEC_LINKED_STATES}


def test_the_transcribed_pull_request_conditions_match_the_spec_document() -> None:
    needs_pr: set[str] = set()
    links_pr: set[str] = set()
    for row in spec_table("Transitions"):
        target, trigger_text = names_in(row[1])[0], row[2]
        if "requires a linked pull request" in trigger_text:
            needs_pr.add(target)
        if "only while no pull request is linked" in trigger_text:
            links_pr.add(target)

    assert needs_pr == {RULES[trigger].target.value for trigger in NEEDS_A_LINKED_PR}
    assert links_pr == {RULES[trigger].target.value for trigger in LINKS_THE_PR}
    assert needs_pr == {
        rule.target.value
        for rule in RULES.values()
        if rule.pull_request is PullRequestCondition.LINKED
    }
    assert links_pr == {
        rule.target.value
        for rule in RULES.values()
        if rule.pull_request is PullRequestCondition.UNLINKED
    }


def test_the_state_diagram_agrees_with_the_transition_table() -> None:
    """The picture is what most readers trust, and a picture nobody checks is how this went wrong.

    Exact in both directions for the ordinary edges. The escalation edges are the one asymmetry:
    the table states them as `any -> BLOCKED` and `any -> FAILED`, and the diagram draws the few
    worth drawing, so those are only required to be a subset.
    """
    escalation = {Trigger.BLOCKED, Trigger.FAILED}
    ordinary = {
        (source.value if source else None, target.value)
        for source, trigger, target in SPEC_TRANSITIONS
        if trigger not in escalation
    }
    escalating = {
        (source.value if source else None, target.value)
        for source, trigger, target in SPEC_TRANSITIONS
        if trigger in escalation
    }
    drawn = spec_diagram_edges()

    assert drawn - escalating == ordinary
    assert drawn <= ordinary | escalating


def test_the_default_cycle_limit_matches_the_operations_table() -> None:
    """The module's one configurable number, so it is read from the spec like everything else."""
    operations = SPEC_DOC.parent / "09-operations.md"
    defaults = {
        names_in(row[0])[0]: row[2].strip("`") for row in spec_table("Configuration", operations)
    }

    assert defaults["MAX_FIX_CYCLES"] == str(DEFAULT_MAX_FIX_CYCLES)
