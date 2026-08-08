"""Playbooks, ACU caps, prompts, tags and the structured-output schema.

Everything Sentinel sends *into* a Devin session is constructed here; sending it is the client's
job (`devin/client.py`) and deciding when to send it is the worker's (`pipeline/worker.py`). The
values below are specified in `docs/05-devin-integration.md` and must not drift from it — the tests
read the tables out of that document and compare.

The prompts follow `docs/adr/2026-08-07-delegate-task-not-steps.md`: they state the issue, the
objective, the definition of done and the constraints, and say nothing about how to investigate or
implement. Class-level standing instructions live in the playbooks, repository facts in the
knowledge notes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final


class UnknownIssueClass(ValueError):
    """An issue class that has no playbook. Callers move the remediation to `BLOCKED`."""


class UnregisteredTag(ValueError):
    """A tag the organisation's registered vocabulary would not accept — a 422 at creation."""


class IssueClass(StrEnum):
    """The classes in `docs/01-overview.md`, matching the `class:<c>` labels on the target repo."""

    SECURITY = "security"
    SECURITY_DEP = "security-dep"
    BUG = "bug"
    FRONTEND_DEP = "frontend-dep"
    FLAKY_TEST = "flaky-test"
    DEPRECATION = "deprecation"
    TYPING = "typing"
    PERF = "perf"


@dataclass(frozen=True, slots=True)
class Playbook:
    """One playbook, its ACU ceiling and the manual effort a remediation of this kind replaces.

    `baseline_hours` is a stated assumption used by the impact panel, not a measured figure, and the
    dashboard labels it as such. `max_acu_limit` is provisional until calibrated against real runs
    (B11 in `docs/blockers.md`).
    """

    name: str
    max_acu_limit: int
    baseline_hours: float


SECURITY_FIX: Final = Playbook("security-fix", max_acu_limit=20, baseline_hours=6.0)
DEP_UPGRADE: Final = Playbook("dep-upgrade", max_acu_limit=10, baseline_hours=2.0)
FLAKY_TEST: Final = Playbook("flaky-test", max_acu_limit=12, baseline_hours=3.0)
DEPRECATION: Final = Playbook("deprecation", max_acu_limit=12, baseline_hours=3.0)

PLAYBOOKS: Final[tuple[Playbook, ...]] = (SECURITY_FIX, DEP_UPGRADE, FLAKY_TEST, DEPRECATION)

PLAYBOOK_BY_CLASS: Final[Mapping[IssueClass, Playbook]] = {
    IssueClass.SECURITY: SECURITY_FIX,
    IssueClass.BUG: SECURITY_FIX,
    IssueClass.SECURITY_DEP: DEP_UPGRADE,
    IssueClass.FRONTEND_DEP: DEP_UPGRADE,
    IssueClass.FLAKY_TEST: FLAKY_TEST,
    IssueClass.TYPING: FLAKY_TEST,
    IssueClass.DEPRECATION: DEPRECATION,
    IssueClass.PERF: DEPRECATION,
}


def issue_class(value: str) -> IssueClass:
    """Coerce a `remediation.issue_class` text column into the enum, or fail loudly."""
    try:
        return IssueClass(value)
    except ValueError:
        known = ", ".join(sorted(c.value for c in IssueClass))
        raise UnknownIssueClass(f"unknown issue class {value!r}; known classes: {known}") from None


def playbook_for(value: str | IssueClass) -> Playbook:
    """The playbook for an issue class. Raises `UnknownIssueClass` rather than defaulting."""
    return PLAYBOOK_BY_CLASS[issue_class(str(value))]


def acu_cap_for(value: str | IssueClass) -> int:
    """`max_acu_limit` for a session on this issue class."""
    return playbook_for(value).max_acu_limit


def baseline_hours_for(value: str | IssueClass) -> float:
    """Assumed engineer-hours this class of remediation replaces."""
    return playbook_for(value).baseline_hours


def playbook_id_for(value: str | IssueClass, playbook_ids: Mapping[str, str]) -> str:
    """Resolve the configured `playbook_id` for an issue class.

    `DEVIN_PLAYBOOK_IDS` is documented as a map of issue class to id, but four playbooks serve eight
    classes, so a map keyed by playbook name is also accepted and needs half the entries. An exact
    issue-class key wins where both are present, which is what lets one class be pointed at a
    different playbook without disturbing the rest.
    """
    playbook = playbook_for(value)
    resolved = playbook_ids.get(str(value)) or playbook_ids.get(playbook.name)
    if not resolved:
        raise UnknownIssueClass(
            f"no playbook id configured for issue class {str(value)!r}; expected a "
            f"DEVIN_PLAYBOOK_IDS entry keyed {str(value)!r} or {playbook.name!r}"
        )
    return resolved


# --- Structured output ---------------------------------------------------------------------------

# Sent as `structured_output_schema` with `structured_output_required: true`, which makes the final
# report a contract rather than prose to be parsed. Reproduced from
# `docs/05-devin-integration.md#structured-output`; each field drives something downstream, so
# neither the field set nor the required list may be trimmed without changing the spec first.
STRUCTURED_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "required": ["outcome", "root_cause", "changes", "tests", "risk"],
    "properties": {
        "outcome": {"enum": ["fixed", "partial", "blocked"]},
        "root_cause": {
            "type": "string",
            "description": "Why the defect exists, not what was changed",
        },
        "changes": {"type": "array", "items": {"type": "string"}},
        "tests": {
            "type": "object",
            "required": ["added", "command", "passed"],
            "properties": {
                "added": {"type": "array", "items": {"type": "string"}},
                "command": {"type": "string"},
                "passed": {"type": "boolean"},
            },
        },
        "risk": {"enum": ["low", "medium", "high"]},
        "blocked_reason": {"type": "string"},
        "pr_url": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

STRUCTURED_OUTPUT_REQUIRED: Final = True


# --- Tags ----------------------------------------------------------------------------------------

# Tags are how a reviewer opens the Devin dashboard and confirms that what Sentinel claims to have
# sent is what Devin received, so they are part of the contract. The vocabulary is registered once
# at bootstrap via `PUT /v3/organizations/{org_id}/tags`; an unregistered tag may be rejected with a
# 422 at session creation (B7), which is why every tag is validated before it is sent rather than
# after the API refuses it.
NAMESPACE_TAG: Final = "sentinel"

# Terminal states of the remediation lifecycle, lowercased — the only values `outcome:` can take.
OUTCOMES: Final[frozenset[str]] = frozenset({"merged", "blocked", "failed"})

TAG_VALUE_PATTERNS: Final[Mapping[str, re.Pattern[str]]] = {
    "repo": re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+"),
    "issue": re.compile(r"[1-9][0-9]*"),
    "class": re.compile("|".join(re.escape(c.value) for c in IssueClass)),
    "run": re.compile(r"[A-Za-z0-9._-]{1,128}"),
    "cycle": re.compile(r"[1-9][0-9]*"),
    "outcome": re.compile("|".join(sorted(OUTCOMES))),
}


def validate_tag(tag: str) -> str:
    """Return `tag` if the registered vocabulary accepts it, otherwise raise `UnregisteredTag`."""
    if tag == NAMESPACE_TAG:
        return tag
    prefix, separator, value = tag.partition(":")
    pattern = TAG_VALUE_PATTERNS.get(prefix) if separator else None
    if pattern is None:
        known = ", ".join([NAMESPACE_TAG, *(f"{p}:" for p in TAG_VALUE_PATTERNS)])
        raise UnregisteredTag(f"tag {tag!r} is outside the vocabulary; registered: {known}")
    if not pattern.fullmatch(value):
        raise UnregisteredTag(f"tag {tag!r} has a value {pattern.pattern!r} would not accept")
    return tag


def session_tags(
    *, repo: str, issue_number: int, issue_class: str | IssueClass, delivery_id: str
) -> list[str]:
    """The tag set every session is created with, in the order the spec tabulates it."""
    return [
        validate_tag(tag)
        for tag in (
            NAMESPACE_TAG,
            f"repo:{repo}",
            f"issue:{issue_number}",
            f"class:{issue_class}",
            f"run:{delivery_id}",
        )
    ]


def cycle_tag(cycle: int) -> str:
    """Appended on each review-fix iteration."""
    return validate_tag(f"cycle:{cycle}")


def outcome_tag(outcome: str) -> str:
    """Appended on reaching a terminal state."""
    return validate_tag(f"outcome:{outcome}")


def session_title(issue_number: int, issue_title: str) -> str:
    """The session `title`, which is what identifies Sentinel's sessions in the Devin dashboard."""
    return f"[{NAMESPACE_TAG}] #{issue_number} {issue_title}"


# --- Prompts -------------------------------------------------------------------------------------

PROMPT_TEMPLATE: Final = """\
GitHub issue #{number} in {repo}: {title}

{issue_body}

Objective
  Diagnose the underlying cause and fix it. Do not paper over the symptom.

Definition of done
  - A branch off {base_branch} with the fix.
  - A regression test that fails before your change and passes after it.
  - The relevant existing test suite passes locally.
  - A pull request against {base_branch} whose description explains the root cause.

Constraints
  - Do not modify generated files or unrelated modules.
  - If you conclude this cannot or should not be fixed, report outcome "blocked"
    with a specific blocked_reason instead of forcing a change."""

# Stands in for an empty issue body: an issue can be filed with a title alone, and a bare blank line
# would read as a truncated prompt.
NO_ISSUE_BODY: Final = "(The issue has no description.)"


def initial_prompt(
    *, issue_number: int, issue_title: str, issue_body: str, repo: str, base_branch: str
) -> str:
    """The prompt a session is created with."""
    return PROMPT_TEMPLATE.format(
        number=issue_number,
        repo=repo,
        title=issue_title,
        issue_body=issue_body.strip() or NO_ISSUE_BODY,
        base_branch=base_branch,
    )


# The log is truncated to the tail because the failure and its traceback are at the end, and a whole
# Superset CI log would crowd out the instruction that follows it.
LOG_EXCERPT_LINES: Final = 100

CI_FAILURE_TEMPLATE: Final = """\
CI failed on {sha}. Failing job: {job_name}.

{log_excerpt}

Diagnose the failure and push a fix to the same branch."""

# The spec templates only the CI-failure message; this is the second loop edge in
# `docs/04-state-machine.md`, written to the same rule — state the new fact, restate the goal.
CHANGES_REQUESTED_TEMPLATE: Final = """\
A reviewer requested changes on {pr_url}.

{review}

Address the review and push a fix to the same branch."""

# Appended to every resume message. The cycle is the one piece of state Devin cannot observe for
# itself, and knowing the budget is what makes "report blocked" a real alternative to churning
# through the remaining cycles.
CYCLE_NOTICE_TEMPLATE: Final = """\

This is fix cycle {cycle} of {max_cycles}. If the goal cannot be reached within the
remaining cycles, report outcome "blocked" with a specific blocked_reason rather
than continuing."""


def _cycle_notice(cycle: int, max_cycles: int) -> str:
    if cycle < 1:
        raise ValueError(f"cycle must be at least 1, got {cycle}")
    if cycle > max_cycles:
        raise ValueError(f"cycle {cycle} exceeds max_cycles {max_cycles}; the remediation failed")
    return CYCLE_NOTICE_TEMPLATE.format(cycle=cycle, max_cycles=max_cycles)


def ci_failure_message(*, sha: str, job_name: str, log: str, cycle: int, max_cycles: int) -> str:
    """Resume message for the `CI_FAILED -> RUNNING` edge."""
    excerpt = "\n".join(log.strip().splitlines()[-LOG_EXCERPT_LINES:])
    body = CI_FAILURE_TEMPLATE.format(sha=sha, job_name=job_name, log_excerpt=excerpt)
    return body + "\n" + _cycle_notice(cycle, max_cycles)


def changes_requested_message(
    *,
    pr_url: str,
    review_body: str,
    inline_comments: Sequence[str] = (),
    cycle: int,
    max_cycles: int,
) -> str:
    """Resume message for the `CHANGES_REQUESTED -> RUNNING` edge.

    `inline_comments` are the reviewer's file comments, already rendered by the caller — a review
    can request changes with an empty body and say everything inline, so both are forwarded.
    """
    review = "\n\n".join(part for part in (review_body.strip(), *inline_comments) if part.strip())
    body = CHANGES_REQUESTED_TEMPLATE.format(
        pr_url=pr_url, review=review or "(The reviewer left no written feedback.)"
    )
    return body + "\n" + _cycle_notice(cycle, max_cycles)
