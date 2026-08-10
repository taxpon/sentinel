"""Typed request and response shapes for the Devin v3 API.

The poller and the worker act on sessions, not on dictionaries: `docs/04-state-machine.md` branches
on the seven session statuses and on `status_detail`, and `docs/05-devin-integration.md` makes the
structured output a contract rather than prose. Both are modelled here so that a missing field is a
parse failure at the boundary rather than a `KeyError` three layers in.

Three things this module fixes, which a hand-built dictionary at each call site would not:

- **The create-session body.** `CreateSessionRequest` carries exactly the ten fields the spec
  tabulates, and takes `structured_output_schema`, `structured_output_required` and `resumable`
  from `playbooks.py` as defaults — a session created without the schema, or without `resumable`,
  would break the structured report and the review-fix loop respectively, and neither failure is
  visible until much later.
- **Degradation as a value.** The endpoints in the spec's degradation table may be unreachable with
  organisation-level credentials (B5, B6). A caller gets `Available` or `Unavailable`, so the
  fallback is a branch it must write rather than an exception it might not catch. `Unavailable`
  carries the fallback text from that table verbatim, which is what the dashboard labels a derived
  figure with.
- **What "unknown" means.** A status outside the seven, or a structured report missing a required
  field, is a protocol violation and fails the parse. Coercing it would hide the one condition that
  says our reading of the API is wrong.

Field shapes marked *unverified* in the comments below are not documented in the spec and cannot be
checked until credentials exist (B8). They are the payloads Sentinel sends or reads for endpoints
the spec names but does not specify field by field.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from sentinel.devin.playbooks import (
    STRUCTURED_OUTPUT_REQUIRED,
    STRUCTURED_OUTPUT_SCHEMA,
)

# --- Sessions ------------------------------------------------------------------------------------


class SessionStatus(StrEnum):
    """The seven statuses `docs/05-devin-integration.md` lists, which the poller maps onto the
    remediation states in `docs/04-state-machine.md`."""

    NEW = "new"
    CLAIMED = "claimed"
    RUNNING = "running"
    EXIT = "exit"
    ERROR = "error"
    SUSPENDED = "suspended"
    RESUMING = "resuming"

    @property
    def is_working(self) -> bool:
        """Whether the session is doing work — the `RUNNING` remediation state in the spec."""
        return self in _WORKING

    @property
    def is_terminal(self) -> bool:
        """Whether Devin is finished with it, successfully or not. Nothing further will change."""
        return self in _TERMINAL


_WORKING: Final = frozenset({SessionStatus.CLAIMED, SessionStatus.RUNNING, SessionStatus.RESUMING})
_TERMINAL: Final = frozenset({SessionStatus.EXIT, SessionStatus.ERROR})

WAITING_FOR_USER: Final = "waiting_for_user"
"""The `status_detail` that distinguishes a session working from one stalled on a question. A
session `running` with this detail is how `docs/05-devin-integration.md` says a stall is detected;
without it `running` covers both, and a stalled session would look busy for ever."""


class Outcome(StrEnum):
    """`structured_output.outcome`. `BLOCKED` forces the `BLOCKED` transition and escalation."""

    FIXED = "fixed"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class Risk(StrEnum):
    """`structured_output.risk`, which orders the dashboard's triage."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SessionTests(BaseModel):
    """The `tests` object of the structured report.

    `added` empty is not an error: it is the acceptance gate in `docs/08-testing.md`, and the
    remediation is flagged for review rather than rejected here.
    """

    model_config = ConfigDict(frozen=True)

    added: tuple[str, ...]
    command: str
    passed: bool


class StructuredOutput(BaseModel):
    """Devin's final report, shaped by `playbooks.STRUCTURED_OUTPUT_SCHEMA`.

    The five fields the schema lists under `required` are required here too. `structured_output_
    required: true` is sent on every session, so a report missing one of them means the contract was
    not honoured — the caller should escalate rather than proceed on a report it cannot read.
    """

    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    root_cause: str
    changes: tuple[str, ...]
    tests: SessionTests
    risk: Risk
    blocked_reason: str | None = None
    pr_url: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


_PULL_REQUEST_NUMBER: Final = re.compile(r"/pull/(?P<number>\d+)(?:[/?#]|$)")
"""The number in a GitHub pull request URL, `https://…/{owner}/{repo}/pull/{number}`.

The host is deliberately not matched. What identifies a pull request is the `/pull/<n>` path
segment, which a GitHub Enterprise install spells the same way; pinning `github.com` would reject a
URL that is perfectly readable for no gain.
"""


def pull_request_number(url: str) -> int | None:
    """The pull request number carried by `url`, or `None` if it carries none."""
    match = _PULL_REQUEST_NUMBER.search(url)
    return None if match is None else int(match["number"])


class PullRequest(BaseModel):
    """One entry of `pull_requests[]`, as observed against the live API on 2026-08-10: `pr_url` and
    `pr_state`, and nothing else. `pr_url` is what links the remediation to the pull request and
    what the dashboard's live table links out to; `pr_state` is not modelled because nothing reads
    it.

    **`url` is not accepted as an alias.** `_unwrap` below tolerates several list envelopes because
    the spec names none and no call had been made — the tolerance stands in for a fact nobody had.
    Here the fact exists: the field is `pr_url`. An alias would buy nothing today and would hide a
    rename tomorrow, because the body would keep parsing under the name the API had stopped sending
    and no one would learn it had moved.
    """

    model_config = ConfigDict(frozen=True)

    pr_url: str

    @property
    def number(self) -> int | None:
        """The pull request number, derived from `pr_url`.

        v3 sends no number anywhere in the session body, and `webhooks._criterion` resolves every
        check-suite and review delivery by `remediation.pr_number` — a null there resolves the whole
        review-fix loop to no remediation, and each delivery is recorded as ignored. So the number
        has to come from the URL.

        A property rather than a validated field, so that a URL this cannot read fails one
        remediation rather than the parse of the whole session: `DevinResponseError` sends the
        remediation to `SESSION_UNREADABLE` and then `FAILED`, which would discard a pull request
        that really exists over the shape of the link to it. The poller logs the miss and records it
        on the remediation's own event instead.
        """
        return pull_request_number(self.pr_url)


def _zero_if_null(value: Any) -> Any:
    # A session that has not been claimed yet reports no consumption at all. Every caller sums or
    # compares this against the ACU budget, so absence is 0.0 rather than a None to guard at each.
    return 0.0 if value is None else value


class Session(BaseModel):
    """A Devin session as v3 returns it, on creation and on every poll.

    Extra fields are ignored rather than rejected: the response carries more than the spec's
    "fields consumed" list, and a field added upstream must not fail a poll. That is not
    hypothetical — the listing observed on 2026-08-10 carried thirteen fields nothing here reads,
    including `created_at` and `updated_at` as **integers**, which is why neither is modelled as a
    timestamp.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    status: SessionStatus
    url: str | None = None
    status_detail: str | None = None
    title: str | None = None
    tags: tuple[str, ...] = ()
    acus_consumed: Annotated[float, BeforeValidator(_zero_if_null)] = 0.0
    pull_requests: tuple[PullRequest, ...] = ()
    structured_output: StructuredOutput | None = None

    @property
    def waiting_for_user(self) -> bool:
        """Whether the session is stalled on a question rather than working."""
        return self.status_detail == WAITING_FOR_USER

    @property
    def pull_request_url(self) -> str | None:
        """The first pull request Devin opened, if any. The link is write-once in the state
        machine, so a later one is recorded as an event and otherwise ignored."""
        return self.pull_requests[0].pr_url if self.pull_requests else None


def _unwrap(payload: Any, *keys: str) -> Any:
    """A list payload that may arrive bare or wrapped under one of `keys`.

    The spec names the listing endpoints but not their envelope, and guessing one of the two would
    fail on the first real call for a reason nobody could read off a traceback. That tolerance paid
    for itself on 2026-08-10: the session listing arrived under `items`, which was not the first
    guess. The other listings remain unobserved (B8).
    """
    if isinstance(payload, Mapping):
        for key in keys:
            if key in payload:
                return payload[key]
    return payload


class SessionPage(BaseModel):
    """The body of `GET /v3/organizations/{org_id}/sessions`.

    Observed on 2026-08-10: the envelope is `items`, beside `end_cursor`, `has_next_page` and
    `total`. The three pagination keys are read by nothing and so modelled by nothing — see
    `DevinClient.list_sessions`, which returns one page.
    """

    model_config = ConfigDict(frozen=True)

    sessions: tuple[Session, ...]

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_list(cls, payload: Any) -> Any:
        return {"sessions": _unwrap(payload, "sessions", "data", "items")}


class CreateSessionRequest(BaseModel):
    """The body of `POST /v3/organizations/{org_id}/sessions`.

    One field per row of the create-session table in `docs/05-devin-integration.md`, in that order,
    and no others: the request body is what a reviewer compares against the Devin dashboard, so an
    extra field here is a discrepancy they would have to chase down.

    The three defaults are the ones nothing downstream can recover from if they are wrong —
    an unschema'd report cannot be parsed, a non-`resumable` session cannot take the review-fix
    loop, and both fail long after the session was created.
    """

    model_config = ConfigDict(frozen=True)

    prompt: str
    title: str
    tags: tuple[str, ...]
    repos: tuple[str, ...]
    playbook_id: str
    knowledge_ids: tuple[str, ...] = ()
    structured_output_schema: dict[str, Any] = STRUCTURED_OUTPUT_SCHEMA
    structured_output_required: bool = STRUCTURED_OUTPUT_REQUIRED
    max_acu_limit: int
    resumable: bool = True


# --- Bootstrap ------------------------------------------------------------------------------------


class KnowledgeNote(BaseModel):
    """A note seeded once at bootstrap, whose id becomes an entry of `DEVIN_KNOWLEDGE_IDS`.

    *Unverified* (B8): `docs/05-devin-integration.md` lists what the four notes say, not the shape
    the endpoint returns. `id` is what the bootstrap script needs and the rest is ignored.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(validation_alias=AliasChoices("id", "note_id", "knowledge_id"))
    name: str | None = None


class Playbook(BaseModel):
    """One playbook of the organisation, as the listing endpoint returns it.

    Only what an operator filling in `DEVIN_PLAYBOOK_IDS` needs: the id the create-session call
    takes, the title it was given in the Devin UI, and which scope it was created at. `body` is
    deliberately not read — the four texts in `docs/playbooks/` are the record of what a playbook
    says, and printing bodies would bury the ids the listing exists to surface.

    The v3 reference spells the id `playbook_id`; `id` is accepted alongside it because every other
    bootstrap response here returns its id under one name or the other and none of them has been
    seen for real (B8).
    """

    model_config = ConfigDict(frozen=True)

    playbook_id: str = Field(validation_alias=AliasChoices("playbook_id", "id"))
    title: str
    access_type: str | None = None


class PlaybookPage(BaseModel):
    """The body of `GET /v3/organizations/{org_id}/playbooks`.

    `has_next_page` is carried rather than followed. The endpoint paginates and the listing asks for
    one page: an operator looking up four ids is told when there are more playbooks than that and
    sent to the web app for the rest, which is cheaper than a cursor loop nothing else needs.
    """

    model_config = ConfigDict(frozen=True)

    playbooks: tuple[Playbook, ...]
    has_next_page: bool = False

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_list(cls, payload: Any) -> Any:
        page = payload if isinstance(payload, Mapping) else {}
        return {
            "playbooks": _unwrap(payload, "items", "playbooks", "data"),
            "has_next_page": page.get("has_next_page") or False,
        }


class TagVocabulary(BaseModel):
    """The body of `GET /v3/enterprise/organizations/{org_id}/tags` — the reference's
    `TagsResponse`, one required `tags` array of strings.

    Read only by `scripts/bootstrap_devin.py --dry-run`, and read because the registration beside
    it is a *replacement*: "Replace the full set of allowed session tags for an organization" is
    what the v3 reference calls that `PUT`, so what a run would take away is as much a part of the
    preview as what it would add — and nothing else in Sentinel can name it.
    """

    model_config = ConfigDict(frozen=True)

    tags: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_list(cls, payload: Any) -> Any:
        return {"tags": _unwrap(payload, "tags", "allowed_tags", "data", "items") or ()}


class Schedule(BaseModel):
    """The nightly vulnerability sweep, created once at bootstrap.

    *Unverified* (B8): the spec tabulates the fields sent, not the response.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(validation_alias=AliasChoices("id", "schedule_id"))
    name: str | None = None


# --- Consumption and metrics ----------------------------------------------------------------------


class DailyConsumption(BaseModel):
    """One day of ACU spend.

    *Unverified* (B8): the spec names the endpoint and what it is for — the budget guard and the
    cost panel — but not its field names.
    """

    model_config = ConfigDict(frozen=True)

    date: dt.date
    acus: float = Field(validation_alias=AliasChoices("acus", "acus_consumed", "acu"))


class Consumption(BaseModel):
    """The body of `GET /v3/organizations/{org_id}/consumption/daily`."""

    model_config = ConfigDict(frozen=True)

    days: tuple[DailyConsumption, ...]

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_list(cls, payload: Any) -> Any:
        return {"days": _unwrap(payload, "days", "consumption", "daily", "data")}

    @property
    def total_acus(self) -> float:
        return sum(day.acus for day in self.days)

    def acus_on(self, day: dt.date) -> float:
        """Spend on one day — what the daily budget guard in `docs/06-event-pipeline.md` compares
        against `DAILY_ACU_BUDGET`. A day Devin did not report is a day nothing was spent."""
        return sum(entry.acus for entry in self.days if entry.date == day)


class SessionMetrics(BaseModel):
    """The body of `GET /v3/enterprise/metrics/sessions` — the aggregates B5 names, which the
    dashboard prefers over the ones Sentinel derives from its own tables.

    *Unverified* (B8): the spec asks for "merged-PR and ACU aggregates" and B5 names the two
    figures, but no credential exists to check the field names against, and this is the one model
    here whose names were invented rather than quoted. Aliases cover the spellings the same figure
    plausibly arrives under; a body that still does not parse degrades the panel to the derived
    figures rather than failing the request, which is the whole point of the capability being
    optional.
    """

    model_config = ConfigDict(frozen=True)

    sessions_with_merged_prs_count: int = Field(
        validation_alias=AliasChoices(
            "sessions_with_merged_prs_count", "sessions_with_merged_prs", "merged_pr_count"
        )
    )
    avg_acus_per_session: float = Field(
        validation_alias=AliasChoices("avg_acus_per_session", "average_acus_per_session")
    )
    sessions_count: int | None = Field(
        default=None, validation_alias=AliasChoices("sessions_count", "total_sessions")
    )


# --- Degradation ----------------------------------------------------------------------------------


class Capability(StrEnum):
    """The rows of the degradation table in `docs/05-devin-integration.md`.

    All of them, including the one no client method serves: `PLAYBOOK_CREATION` is configuration
    rather than a runtime call — the four playbooks are created in the Devin UI and their ids
    supplied through `DEVIN_PLAYBOOK_IDS` (B6) — but the bootstrap script reports on the same
    vocabulary the dashboard labels figures with, and two vocabularies would drift.

    `PLAYBOOK_DISCOVERY` is the *read* beside that write: creating a playbook is unavailable, but
    finding the id of one somebody created by hand may not be, and that is what
    `make devin-playbooks` asks. `TAG_DISCOVERY` is the same shape again, and the one whose absence
    costs something: without it `--dry-run` cannot say which tags the registration would *remove*.
    """

    SESSION_METRICS = "session_metrics"
    ACU_SPEND = "acu_spend"
    PLAYBOOK_CREATION = "playbook_creation"
    PLAYBOOK_DISCOVERY = "playbook_discovery"
    TAG_DISCOVERY = "tag_discovery"

    @property
    def fallback(self) -> str:
        """What to do instead, quoted from the degradation table."""
        return FALLBACKS[self]


FALLBACKS: Final[Mapping[Capability, str]] = {
    Capability.SESSION_METRICS: "Compute from Sentinel's own `remediation` table",
    Capability.ACU_SPEND: "Sum `acus_consumed` across sessions",
    Capability.PLAYBOOK_CREATION: (
        "Create playbooks in the Devin UI and supply the ids via `PLAYBOOK_IDS` env config"
    ),
    Capability.PLAYBOOK_DISCOVERY: (
        "Open each playbook in the Devin web app and read its id from the page"
    ),
    Capability.TAG_DISCOVERY: (
        "Read the organisation's allowed tags in the Devin web app before registering, since the "
        "registration replaces them"
    ),
}


class Unavailability(StrEnum):
    """Why a capability is unavailable. Logged, and reported by `make bootstrap-devin`."""

    NOT_CONFIGURED = "not_configured"
    """No `DEVIN_ENTERPRISE_ID` — the deployment does not claim enterprise scope, so the call is
    not attempted at all."""

    FORBIDDEN = "forbidden"
    """`403` — the service user lacks the permission (`ViewAccountMetrics` for B5)."""

    NOT_FOUND = "not_found"
    """`404` — the endpoint is not exposed to this organisation."""

    UNREADABLE = "unreadable"
    """Devin answered, and the body is not one the model accepts. On these two endpoints — and only
    these — that degrades rather than raising: their field names are unverified until B8 is
    resolved, so a name guessed wrong is the likeliest way for them to fail, and the spec's answer
    to an unavailable capability is a labelled fallback rather than a panel that errors."""


@dataclass(frozen=True, slots=True)
class Available[T]:
    """Devin answered, and the answer parsed."""

    value: T
    available: Literal[True] = True


@dataclass(frozen=True, slots=True)
class Unavailable:
    """Devin cannot serve this capability with these credentials.

    A value rather than an exception: the fallback is defined for every row of the table, so a
    permission gap is a branch the caller takes, not a failure that reaches the worker and fails a
    job. `fallback` is the text of the table, and the dashboard labels the figure it derives with
    it so a reader can tell which numbers came from Devin.
    """

    capability: Capability
    reason: Unavailability
    status_code: int | None = None
    available: Literal[False] = False

    @property
    def fallback(self) -> str:
        return self.capability.fallback


type Degradable[T] = Available[T] | Unavailable
"""What a degradable endpoint returns. Narrow it with `isinstance(result, Unavailable)`, or on the
`available` tag:

    spend = await devin.daily_consumption()
    total = spend.value.total_acus if spend.available else derive_from_remediations()
"""
