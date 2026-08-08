"""Remediation lifecycle state machine.

A pure encoding of the transition table in `docs/04-state-machine.md`: no database, no HTTP, no
clock. Callers read the current state and cycle, apply a trigger, and persist whatever comes back
in their own transaction — one `remediation_event` per returned transition.

The table is indexed by trigger rather than by `(from, to)` pair, because every trigger in the spec
leads to exactly one state; what varies is which states it may be applied from.
"""

from collections.abc import Mapping
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

DEFAULT_MAX_FIX_CYCLES: Final = 3
"""Spec default for `MAX_FIX_CYCLES`. Deployments override it through configuration."""

CYCLE_LIMIT_EXHAUSTED: Final = "cycle_limit_exhausted"
"""Reason recorded when the machine forces `FAILED` instead of another fix cycle."""


@dataclass(frozen=True, slots=True)
class Rule:
    """How one trigger behaves: where it leads, and the states it may be applied from."""

    target: State
    sources: frozenset[State | None]
    increments_cycle: bool = False


RULES: Final[Mapping[Trigger, Rule]] = {
    Trigger.ISSUE_LABELLED: Rule(State.QUEUED, frozenset({None})),
    Trigger.SESSION_CREATED: Rule(State.SESSION_CREATED, frozenset({State.QUEUED})),
    Trigger.SESSION_RUNNING: Rule(State.RUNNING, frozenset({State.SESSION_CREATED})),
    Trigger.PR_OPENED: Rule(State.PR_OPENED, frozenset({State.RUNNING})),
    Trigger.CHECK_SUITE_REQUESTED: Rule(
        State.CI_RUNNING, frozenset({State.PR_OPENED, State.RUNNING})
    ),
    Trigger.CHECK_SUITE_SUCCEEDED: Rule(
        State.CI_PASSED, frozenset({State.CI_RUNNING, State.RUNNING})
    ),
    Trigger.CHECK_SUITE_FAILED: Rule(State.CI_FAILED, frozenset({State.CI_RUNNING, State.RUNNING})),
    Trigger.SESSION_RESUMED: Rule(
        State.RUNNING,
        frozenset({State.CI_FAILED, State.CHANGES_REQUESTED}),
        increments_cycle=True,
    ),
    Trigger.REVIEW_REQUESTED: Rule(State.IN_REVIEW, frozenset({State.CI_PASSED})),
    Trigger.CHANGES_REQUESTED: Rule(State.CHANGES_REQUESTED, frozenset({State.IN_REVIEW})),
    Trigger.PR_MERGED: Rule(State.MERGED, frozenset({State.IN_REVIEW})),
    Trigger.BLOCKED: Rule(State.BLOCKED, frozenset(NON_TERMINAL_STATES)),
    Trigger.FAILED: Rule(State.FAILED, frozenset(NON_TERMINAL_STATES)),
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
    """Raised when a trigger is applied to a state the spec does not allow it from."""

    def __init__(self, from_state: State | None, trigger: Trigger, to_state: State) -> None:
        self.from_state = from_state
        self.trigger = trigger
        self.to_state = to_state
        allowed = ", ".join(sorted(_label(s) for s in RULES[trigger].sources))
        super().__init__(
            f"illegal transition {_label(from_state)} -> {to_state} "
            f"on trigger {trigger}: {to_state} is reachable only from {allowed}"
        )


def _label(state: State | None) -> str:
    return "(new)" if state is None else str(state)


def is_legal(state: State | None, trigger: Trigger) -> bool:
    """Whether `trigger` moves a remediation in `state`. False for terminal states, which absorb."""
    return state in RULES[trigger].sources


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
    max_fix_cycles: int = DEFAULT_MAX_FIX_CYCLES,
) -> Transition:
    """Apply `trigger` to a remediation in `state` with `cycle` fix cycles behind it.

    A terminal state absorbs every trigger: the result reports the state unchanged with
    `absorbed=True`, so a webhook arriving late is recorded rather than raising. Otherwise a
    trigger not legal from `state` raises `IllegalTransitionError`.

    Resuming a session is the only trigger that increments `cycle`. One past `max_fix_cycles` the
    machine yields `FAILED` with reason `cycle_limit_exhausted` instead, so no caller has to carry
    the limit itself.
    """
    if state is not None and state in TERMINAL_STATES:
        return Transition(state, trigger, state, cycle, absorbed=True)

    rule = RULES[trigger]
    if state not in rule.sources:
        raise IllegalTransitionError(state, trigger, rule.target)

    if not rule.increments_cycle:
        return Transition(state, trigger, rule.target, cycle)

    if cycle + 1 > max_fix_cycles:
        return Transition(state, trigger, State.FAILED, cycle, reason=CYCLE_LIMIT_EXHAUSTED)
    return Transition(state, trigger, rule.target, cycle + 1)
