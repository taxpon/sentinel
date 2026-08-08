"""Remediation lifecycle state machine.

A pure encoding of the transition table in `docs/04-state-machine.md`: no database, no HTTP, no
clock. Callers read the current state and cycle, apply a trigger, and persist whatever comes back
in their own transaction — one `remediation_event` per returned transition.

The table is indexed by trigger rather than by `(from, to)` pair, because every trigger in the spec
leads to exactly one state; what varies is which states it may be applied from, and whether a pull
request has been linked yet.

Two things callers must know:

- `Trigger.ISSUE_LABELLED` is legal only from `None`. The "label removed and re-added" case that
  `docs/06-event-pipeline.md` assigns to the domain deduplication layer therefore **raises** here
  rather than being ignored: the remediation already exists, and re-labelling is not a transition.
  Resolve it before applying the trigger — `INSERT … ON CONFLICT DO NOTHING` says whether the row is
  new, and `is_legal` answers the same question directly.
- `automatic_trigger` is not applied for you. Entering `CI_PASSED` obliges the caller to apply the
  trigger it returns, and nothing in this module enforces that.
"""

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class State(StrEnum):
    """The states a remediation can be in. Stored verbatim in `remediation.state`."""

    QUEUED = "QUEUED"
    SESSION_CREATED = "SESSION_CREATED"
    RUNNING = "RUNNING"
    PR_OPENED = "PR_OPENED"
    CI_RUNNING = "CI_RUNNING"
    CI_FAILED = "CI_FAILED"
    CI_PASSED = "CI_PASSED"
    IN_REVIEW = "IN_REVIEW"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    MERGED = "MERGED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class Trigger(StrEnum):
    """What moves a remediation. One per trigger in the spec's transition table."""

    ISSUE_LABELLED = "issue_labelled"
    SESSION_CREATED = "session_created"
    SESSION_RUNNING = "session_running"
    PR_OPENED = "pr_opened"
    CHECK_SUITE_REQUESTED = "check_suite_requested"
    CHECK_SUITE_SUCCEEDED = "check_suite_succeeded"
    CHECK_SUITE_FAILED = "check_suite_failed"
    SESSION_RESUMED = "session_resumed"
    REVIEW_REQUESTED = "review_requested"
    CHANGES_REQUESTED = "changes_requested"
    PR_MERGED = "pr_merged"
    BLOCKED = "blocked"
    FAILED = "failed"


TERMINAL_STATES: Final = frozenset({State.MERGED, State.BLOCKED, State.FAILED})
NON_TERMINAL_STATES: Final = frozenset(State) - TERMINAL_STATES

PRE_PULL_REQUEST_STATES: Final = frozenset({State.QUEUED, State.SESSION_CREATED})
LINKED_STATES: Final = NON_TERMINAL_STATES - PRE_PULL_REQUEST_STATES
"""Every non-terminal state a remediation with a linked pull request can be sitting in.

Sentinel does not choose when a check suite reports, so a `check_suite` event can arrive in any of
these. All three are legal from all of them — see `Rule.absorbed_from` for the ones that record the
event without moving.
"""

DEFAULT_MAX_FIX_CYCLES: Final = 3
"""Spec default for `MAX_FIX_CYCLES`. Deployments override it through configuration."""

CYCLE_LIMIT_EXHAUSTED: Final = "cycle_limit_exhausted"
"""Reason recorded when the machine forces `FAILED` instead of another fix cycle."""


class PullRequestCondition(StrEnum):
    """What a trigger needs of the remediation's pull request link.

    The two loop-widened rows of the spec table are bounded by this rather than by the lap count.
    `LINKED` keeps `PR_OPENED` on every path into the CI states; `UNLINKED` makes the link
    write-once, so a poller re-reading `pull_requests[]` cannot move the state backwards out of the
    loop or re-stamp `pr_opened_at`.
    """

    ANY = "any"
    LINKED = "linked"
    UNLINKED = "unlinked"


@dataclass(frozen=True, slots=True)
class Rule:
    """How one trigger behaves: where it leads, and the conditions it may be applied under.

    `absorbed_from` is the subset of `sources` where the trigger is legal but carries nothing to
    act on — a second failure while already in `CI_FAILED`, a success on a pull request that has
    already been green. The event is recorded and the state left alone, rather than raising (the
    event is real) or moving (which would walk the remediation backwards).
    """

    target: State
    sources: AbstractSet[State | None]
    increments_cycle: bool = False
    pull_request: PullRequestCondition = PullRequestCondition.ANY
    absorbed_from: AbstractSet[State] = frozenset()


RULES: Final[Mapping[Trigger, Rule]] = {
    Trigger.ISSUE_LABELLED: Rule(State.QUEUED, frozenset({None})),
    Trigger.SESSION_CREATED: Rule(State.SESSION_CREATED, frozenset({State.QUEUED})),
    Trigger.SESSION_RUNNING: Rule(State.RUNNING, frozenset({State.SESSION_CREATED})),
    Trigger.PR_OPENED: Rule(
        State.PR_OPENED,
        frozenset({State.RUNNING}),
        pull_request=PullRequestCondition.UNLINKED,
    ),
    # A suite starting again is not news: pulling a remediation out of review or out of the fix
    # loop for it would lose the state that matters. Its conclusion will follow.
    Trigger.CHECK_SUITE_REQUESTED: Rule(
        State.CI_RUNNING,
        LINKED_STATES,
        pull_request=PullRequestCondition.LINKED,
        absorbed_from=frozenset(
            {
                State.CI_RUNNING,
                State.CI_FAILED,
                State.CI_PASSED,
                State.IN_REVIEW,
                State.CHANGES_REQUESTED,
            }
        ),
    ),
    # `ci_green_at` is the *first* successful suite, so a later success must neither re-stamp it
    # nor drag a remediation back out of review.
    Trigger.CHECK_SUITE_SUCCEEDED: Rule(
        State.CI_PASSED,
        LINKED_STATES,
        pull_request=PullRequestCondition.LINKED,
        absorbed_from=frozenset({State.CI_PASSED, State.IN_REVIEW, State.CHANGES_REQUESTED}),
    ),
    # A failure is news almost everywhere — including `IN_REVIEW`, where nothing else would engage
    # the fix loop. Not in `CHANGES_REQUESTED`, which already has a resume pending that will
    # produce a fresh suite, nor twice over in `CI_FAILED`.
    Trigger.CHECK_SUITE_FAILED: Rule(
        State.CI_FAILED,
        LINKED_STATES,
        pull_request=PullRequestCondition.LINKED,
        absorbed_from=frozenset({State.CI_FAILED, State.CHANGES_REQUESTED}),
    ),
    Trigger.SESSION_RESUMED: Rule(
        State.RUNNING,
        frozenset({State.CI_FAILED, State.CHANGES_REQUESTED}),
        increments_cycle=True,
    ),
    Trigger.REVIEW_REQUESTED: Rule(State.IN_REVIEW, frozenset({State.CI_PASSED})),
    Trigger.CHANGES_REQUESTED: Rule(State.CHANGES_REQUESTED, frozenset({State.IN_REVIEW})),
    Trigger.PR_MERGED: Rule(State.MERGED, frozenset({State.IN_REVIEW})),
    Trigger.BLOCKED: Rule(State.BLOCKED, NON_TERMINAL_STATES),
    Trigger.FAILED: Rule(State.FAILED, NON_TERMINAL_STATES),
}

_AUTOMATIC: Final[Mapping[State, Trigger]] = {State.CI_PASSED: Trigger.REVIEW_REQUESTED}


@dataclass(frozen=True, slots=True)
class Transition:
    """The outcome of applying a trigger: what to write, and what the caller should act on."""

    from_state: State | None
    trigger: Trigger
    to_state: State
    cycle: int
    absorbed: bool = False
    reason: str | None = None

    @property
    def moved(self) -> bool:
        """True when the remediation changed state, so the caller has work to do."""
        return not self.absorbed


class IllegalTransitionError(Exception):
    """Raised when a trigger is applied under conditions the spec does not allow."""

    def __init__(
        self,
        from_state: State | None,
        trigger: Trigger,
        to_state: State,
        because: str | None = None,
    ) -> None:
        self.from_state = from_state
        self.trigger = trigger
        self.to_state = to_state
        if because is None:
            allowed = ", ".join(sorted(_label(s) for s in RULES[trigger].sources))
            because = f"{to_state} is reachable only from {allowed}"
        super().__init__(
            f"illegal transition {_label(from_state)} -> {to_state} on trigger {trigger}: {because}"
        )


def _label(state: State | None) -> str:
    return "(new)" if state is None else str(state)


def is_legal(state: State | None, trigger: Trigger, *, pr_linked: bool = False) -> bool:
    """Whether `trigger` moves a remediation in `state`.

    False for a terminal state and for a pull request already linked, both of which `transition`
    absorbs rather than rejects, so callers with no reason to distinguish the two can guard on this
    alone.
    """
    rule = RULES[trigger]
    if state not in rule.sources or state in rule.absorbed_from:
        return False
    if rule.pull_request is PullRequestCondition.LINKED:
        return pr_linked
    if rule.pull_request is PullRequestCondition.UNLINKED:
        return not pr_linked
    return True


def automatic_trigger(state: State) -> Trigger | None:
    """The trigger that entering `state` itself fires, applied by the caller as a second step.

    `CI_PASSED` is the only such state: the spec moves it straight on to `IN_REVIEW`. It stays a
    separate call so that each transition still writes its own `remediation_event`.
    """
    return _AUTOMATIC.get(state)


def transition(
    state: State | None,
    trigger: Trigger,
    *,
    cycle: int = 0,
    pr_linked: bool = False,
    max_fix_cycles: int = DEFAULT_MAX_FIX_CYCLES,
) -> Transition:
    """Apply `trigger` to a remediation in `state`, `cycle` fix cycles in.

    `pr_linked` is `remediation.pr_url IS NOT NULL`. It decides the two conditional rows of the
    spec table: the CI triggers need a linked pull request, and `PR_OPENED` needs the absence of
    one.

    Three situations absorb — the result reports the state and cycle unchanged with `absorbed=True`,
    so the caller records a `remediation_event` and does nothing else. A terminal state absorbs
    every trigger, so a late webhook is not an error. A `PR_OPENED` for a pull request already
    linked absorbs from any state, so the poller re-reading `pull_requests[]` on every poll neither
    raises nor re-applies the side effect. And a trigger applied from one of its `absorbed_from`
    states absorbs, which is how a `check_suite` event that carries no new verdict is recorded
    without walking the remediation backwards.

    Anything else the spec does not allow raises `IllegalTransitionError`.

    Resuming a session is the only trigger that increments `cycle`. One past `max_fix_cycles` the
    machine yields `FAILED` with reason `cycle_limit_exhausted` instead, so no caller has to carry
    the limit itself.
    """
    if state is not None and state in TERMINAL_STATES:
        return Transition(state, trigger, state, cycle, absorbed=True)

    rule = RULES[trigger]

    if state is not None and rule.pull_request is PullRequestCondition.UNLINKED and pr_linked:
        return Transition(state, trigger, state, cycle, absorbed=True)

    if state not in rule.sources:
        raise IllegalTransitionError(state, trigger, rule.target)

    if rule.pull_request is PullRequestCondition.LINKED and not pr_linked:
        raise IllegalTransitionError(
            state,
            trigger,
            rule.target,
            because=f"{trigger} needs a pull request linked to the remediation",
        )

    if state is not None and state in rule.absorbed_from:
        return Transition(state, trigger, state, cycle, absorbed=True)

    if not rule.increments_cycle:
        return Transition(state, trigger, rule.target, cycle)

    if cycle + 1 > max_fix_cycles:
        return Transition(state, trigger, State.FAILED, cycle, reason=CYCLE_LIMIT_EXHAUSTED)
    return Transition(state, trigger, rule.target, cycle + 1)
