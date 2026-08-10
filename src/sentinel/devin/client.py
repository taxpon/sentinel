"""Async HTTP client for the Devin v3 API.

Every path is a `/v3/...` path: v1 and v2 are not reachable from here, which
`docs/adr/2026-08-07-devin-v3-only.md` requires and a test over `ENDPOINTS` asserts. The endpoints
are the table in `docs/05-devin-integration.md#endpoints-used`, no more and no fewer — the worker
and the poller use the first six, `make bootstrap-devin` the next three, `make devin-playbooks` the
playbook listing, and the dashboard the last.

What this module decides, so that no caller has to:

- **The request body.** `create_session` takes the facts of an issue and builds the body from
  `playbooks.py` — prompt, title, tags, playbook id, ACU cap, structured-output schema. The tag set
  in particular is built through `session_tags`, never assembled by a caller: a tag outside the
  registered vocabulary is a `422` at creation (B7), and this is where that is made impossible
  rather than unlikely. Tags that are not a session's go through `registered_tag` instead, which
  applies the vocabulary Devin was given rather than the stricter rule a session needs.
- **What is worth retrying.** `429` and `5xx` and a connection that never answered are retried with
  exponential backoff and jitter, honouring `Retry-After`. Every other `4xx` raises immediately —
  a body Devin rejected as malformed will not become well-formed, and retrying it only spends
  quota. The exception carries the response body, which `docs/06-event-pipeline.md` records in
  `remediation_event.detail`. **Creating a session is the one exception and is sent exactly once**
  — see `create_session` and
  `docs/adr/2026-08-11-a-session-is-adopted-before-it-is-created.md`.
- **What is a degradation rather than a failure.** The enterprise-scoped and consumption endpoints
  answer `403` or `404` when the credentials do not carry the scope (B5, B6), and their field names
  are unverified until credentials exist (B8). A refusal, an absent enterprise id and a body that
  does not parse all become an `Unavailable` the caller branches on, not an exception that fails a
  job. Everything else, including `401`, raises: a rejected token is a fault to fix, not a
  capability to work around.

The token is a `SecretStr` and is read exactly once, into the `Authorization` header httpx masks in
its own repr. It is not stored on this object, does not appear in `repr()`, and cannot reach an
exception message — the messages are built from the method, the *templated* path and the response
body, and a transport error is rendered as its exception *class*, because the exception itself
renders the URL and a proxy URL can carry credentials. Nothing here logs a header or a request
body, which is asserted on the event dict *before* the redaction processor in
`observability/logging.py` runs: that processor is the second line of defence
(`docs/07-observability.md`), and a test that only reads its output would hold this module to
nothing.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import httpx
from pydantic import BaseModel, ValidationError

from sentinel.config import DevinSettings
from sentinel.devin.playbooks import (
    NAMESPACE_TAG,
    TAG_PREFIXES,
    IssueClass,
    UnregisteredTag,
    acu_cap_for,
    initial_prompt,
    playbook_id_for,
    session_identity,
    session_tags,
    session_title,
    validate_tag,
)
from sentinel.devin.schemas import (
    Available,
    Capability,
    Consumption,
    CreateSessionRequest,
    Degradable,
    KnowledgeNote,
    PlaybookPage,
    Session,
    SessionMetrics,
    SessionPage,
    TagVocabulary,
    Unavailability,
    Unavailable,
)
from sentinel.observability.logging import get_logger
from sentinel.observability.prom import METRICS, DevinOutcome, Metrics

log = get_logger(__name__)

# --- The route table -----------------------------------------------------------------------------

# Templated paths, formatted with the ids at call time. They are also the `endpoint` label on the
# latency histogram, which is why the template is what is passed around: a path with the session id
# substituted in would be a new Prometheus time series per session.
_ORGANIZATION: Final = "/v3/organizations/{org_id}"

SESSIONS: Final = f"{_ORGANIZATION}/sessions"
SESSION: Final = f"{SESSIONS}/{{session_id}}"
SESSION_MESSAGES: Final = f"{SESSION}/messages"
SESSION_TAGS: Final = f"{SESSION}/tags"
ORGANIZATION_TAGS: Final = f"{_ORGANIZATION}/tags"
ALLOWED_TAGS: Final = "/v3/enterprise/organizations/{org_id}/tags"
"""Where the v3 reference documents an organisation's allowed-tag vocabulary — which is read here
and is **not** where `ORGANIZATION_TAGS` above writes it.

The two are one resource under two paths, or they are two resources, and no credentials exist to
find out (B8). What the reference states is the enterprise-prefixed path, for every method it lists
on the vocabulary — `GET`, `PUT` (replace the full set), `POST` (append), and two `DELETE`s — each
requiring `ManageEnterpriseSettings`. `register_tags` sends the organisation-scoped path this
codebase has used since the vocabulary was first specified, and the reference lists no such path.

Reading where the reference says to read is the half of that which changes nothing. Moving the
write is a change to what `make bootstrap-devin` does to a real organisation, and belongs to
whoever owns that decision rather than to the preview that surfaced it —
`scripts/bootstrap_devin.py --dry-run` reports the discrepancy where an operator will see it."""
KNOWLEDGE_NOTES: Final = f"{_ORGANIZATION}/knowledge/notes"
PLAYBOOKS: Final = f"{_ORGANIZATION}/playbooks"
CONSUMPTION_DAILY: Final = f"{_ORGANIZATION}/consumption/daily"
ENTERPRISE_SESSION_METRICS: Final = "/v3/enterprise/metrics/sessions"

ENDPOINTS: Final[frozenset[str]] = frozenset(
    {
        SESSIONS,
        SESSION,
        SESSION_MESSAGES,
        SESSION_TAGS,
        ORGANIZATION_TAGS,
        ALLOWED_TAGS,
        KNOWLEDGE_NOTES,
        PLAYBOOKS,
        CONSUMPTION_DAILY,
        ENTERPRISE_SESSION_METRICS,
    }
)
"""Every path this client can reach. The spec's endpoint table and this set are compared in the
tests, in both directions, so an endpoint added here without a row there fails the suite."""

TIMEOUT: Final = httpx.Timeout(30.0)
"""30 s connect and read, from `docs/05-devin-integration.md#client-behaviour`. Session creation is
never on the webhook request path, so it is free to take that long."""

MAX_ERROR_BODY: Final = 2000
"""How much of a rejected response is kept on the exception. Enough for a validation error naming
the offending field; short enough to store in `remediation_event.detail` and read in a log line."""

DEGRADES: Final[Mapping[int, Unavailability]] = {
    403: Unavailability.FORBIDDEN,
    404: Unavailability.NOT_FOUND,
}
"""Statuses that mean "this deployment does not have this capability" rather than "this request was
wrong". `401` is deliberately absent: a rejected token is a misconfiguration that must be fixed,
and silently falling back to derived figures would hide it."""


# --- Tags ----------------------------------------------------------------------------------------


def registered_tag(tag: str) -> str:
    """Return `tag` if the organisation's *registered vocabulary* accepts it.

    There are two rules, and they are not the same one. `playbooks.validate_tag` is the **session**
    rule: it additionally requires a `class:` value to name an issue class Sentinel handles, because
    a session created for a class with no playbook is a remediation nothing downstream can finish.
    What is registered with Devin is only the namespace tag and the bare prefixes of
    `TAG_PREFIXES`, which is a wider net: Devin accepts any value behind a registered prefix.

    The difference is not hypothetical: a `class:` value naming something that is not one of
    Sentinel's issue classes — a session somebody else in the organisation created under the same
    registered prefix — is a legitimate thing to *search* for and an illegitimate thing to create a
    session with. Anything that is not creating a session is checked here instead.

    Derived from the same two exports the bootstrap registration sends, so the check and the
    registration cannot drift apart.
    """
    prefix, separator, value = tag.partition(":")
    if tag == NAMESPACE_TAG or (separator and value and prefix in TAG_PREFIXES):
        return tag
    known = ", ".join([NAMESPACE_TAG, *(f"{name}:" for name in TAG_PREFIXES)])
    raise UnregisteredTag(f"tag {tag!r} is outside the vocabulary; registered: {known}")


# --- Errors --------------------------------------------------------------------------------------


class DevinError(Exception):
    """Anything this client could not complete. Never carries the token."""

    @property
    def retryable(self) -> bool:
        """Whether sending the same request again could plausibly succeed."""
        return False


class DevinTransportError(DevinError):
    """The request never got a response — connection refused, DNS, a timeout."""

    @property
    def retryable(self) -> bool:
        return True


class DevinAPIError(DevinError):
    """Devin answered, and refused.

    `body` is the response, truncated: `docs/06-event-pipeline.md` records it in
    `remediation_event.detail`, which is what makes a `422` from an unregistered tag (B7)
    diagnosable from Sentinel's own audit trail rather than only from the Devin dashboard.
    """

    def __init__(self, *, method: str, endpoint: str, status_code: int, body: str) -> None:
        self.method = method
        self.endpoint = endpoint
        self.status_code = status_code
        self.body = body[:MAX_ERROR_BODY]
        super().__init__(f"{method} {endpoint} failed with {status_code}: {self.body}")

    @property
    def retryable(self) -> bool:
        return self.status_code == 429 or self.status_code >= 500


class DevinResponseError(DevinError):
    """Devin accepted the request and answered with something v3 does not document.

    Not retryable, and distinct from `DevinAPIError` on purpose: a session whose structured report
    is missing a required field will report the same thing on the next poll, so the caller
    escalates it instead of polling it for ever.
    """


class SessionLookupIncomplete(DevinError):
    """The adopt-or-create lookup ran out of pages before it could answer, so nothing was created.

    Reached only when the listing keeps reporting `has_next_page` for `LOOKUP_PAGE_LIMIT` pages
    without the `tags` filter having narrowed anything — which means the server-side filter is not
    doing what `SessionsQueryParams.tags` says it does. Not retryable: the next attempt walks the
    same pages to the same place.

    Raising is the point. The alternative to an answer is not "create one anyway" — that is the
    defect this whole path exists to prevent — so a lookup that cannot answer fails the job, and
    the remediation escalates to a human with a session count of zero rather than of two.
    """


# --- Retry policy --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with jitter, as `docs/05-devin-integration.md#client-behaviour` states.

    Bounded and short by design. The job queue retries too, with its attempt count persisted and
    `run_after` growing from ten seconds to ten minutes
    (`docs/06-event-pipeline.md#reliability-policy`); this layer only absorbs the rate limit and the
    blip that would otherwise cost a whole job attempt, and hands everything else back.

    `max_total_delay` is what makes that bound a wall-clock one. `max_delay` caps a *single* sleep,
    so the total spent waiting is only bounded by it because `attempts` happens to be small — and a
    `Retry-After` of an hour, or an attempt count raised later, would quietly turn a job into a
    lease that expires while it is still sleeping. With the defaults the worst case is
    `attempts * TIMEOUT + max_total_delay` = 3 * 30 s + 60 s = 150 s, against a
    `JOB_LEASE_TIMEOUT_SECONDS` of 900.
    """

    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 30.0
    max_total_delay: float = 60.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError(f"attempts must be at least 1, got {self.attempts}")

    def ceiling(self, attempt: int) -> float:
        """The longest this attempt waits before the next one."""
        return min(self.base_delay * 2.0 ** (attempt - 1), self.max_delay)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """`Retry-After` in seconds, if the response gave one we can act on.

    Only the delta-seconds form is read. The HTTP-date form would need the server's clock to agree
    with ours, and a skewed clock turns a two-second wait into an hour.
    """
    header = response.headers.get("retry-after")
    if header is None:
        return None
    try:
        seconds = float(header.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


DEFAULT_RETRY: Final = RetryPolicy()

SEND_ONCE: Final = RetryPolicy(attempts=1)
"""The policy `POST /v3/organizations/{org_id}/sessions` is sent under: one attempt, no retry.

A read timeout is not a failed request. On 2026-08-11 Devin was overloaded and stopped answering
the create within 30 s; the requests had arrived and the sessions existed, so three attempts made
three sessions and three issues ended up with three each. Nothing in `SessionCreateRequest` takes an
idempotency key, so a resend cannot be told from a first send by anything on the wire.

Retrying here is therefore not merely unhelpful, it is the bug. Recovery is `create_session`'s
adopt-or-create instead, which is also what covers the *job* being retried minutes later — a
narrower policy on its own would only move the duplicate one layer out.
"""

LOOKUP_PAGE_SIZE: Final = 200
"""`SessionsQueryParams.first`, at the maximum the reference gives it — default 100, minimum 1,
maximum 200, recorded in the endpoint table of `docs/05-devin-integration.md`. The lookup wants as
few round trips as the endpoint will give it."""

LOOKUP_PAGE_LIMIT: Final = 4
"""How many pages `find_session` walks before it gives up and raises `SessionLookupIncomplete`.

With the `tags` filter working, an organisation has at most a handful of sessions per remediation
and the first page ends the walk. This bound only bites when the filter is being ignored.

**It is derived from the job lease, not chosen.** The walk runs inside a claimed job, and a lookup
that outlasts `JOB_LEASE_TIMEOUT_SECONDS` lets a second worker claim the same job — and if both
workers look before either posts, both create. That is the reclaim hazard this design closes
everywhere else, reintroduced by the lookup meant to close it.

A page is a read and keeps every retry a read has (`DEFAULT_RETRY`), so one page can succeed on its
third attempt and cost `attempts * TIMEOUT + max_total_delay` = 150 s. Four of those plus one create
is 630 s, against a lease of 900 s. `test_the_lookup_cannot_outlast_the_lease_that_protects_the_job`
computes that from the constants rather than restating it, so raising either number without raising
the lease fails the suite.

Four pages is 800 sessions examined, which is not the constraint it looks like: when the filter
works one page ends the walk, and when it does not the answer is to find out why rather than to
scan further.
"""

Sleep = Callable[[float], Awaitable[None]]

PathParams = Mapping[str, str]


# --- The client ----------------------------------------------------------------------------------


class DevinClient:
    """The Devin v3 API, as the worker, the poller and the bootstrap script use it.

        async with DevinClient(settings) as devin:
            session = await devin.create_session(
                issue_number=42, issue_title="…", issue_body="…",
                issue_class="security", delivery_id=run_id,
            )

    One `httpx.AsyncClient` is held for the life of the object, so connections are reused across a
    poll cycle. `sleep` and `rng` are injectable so that the backoff can be asserted without a test
    waiting for it.

    `DevinSettings` rather than `Settings`: everything read here is Devin's own or the target
    repository's, and `scripts/bootstrap_devin.py` is entitled to a configuration that stops there.
    A service hands over its whole `Settings`, which is one.
    """

    def __init__(
        self,
        settings: DevinSettings,
        *,
        retry: RetryPolicy = DEFAULT_RETRY,
        metrics: Metrics = METRICS,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._retry = retry
        self._metrics = metrics
        self._sleep = sleep
        self._rng = random.Random() if rng is None else rng
        self._client = httpx.AsyncClient(
            base_url=settings.devin_api_base,
            timeout=TIMEOUT,
            headers={
                # Read once, here. Nothing else on this object holds the token, and httpx masks
                # `Authorization` in the repr of its own headers.
                "Authorization": f"Bearer {settings.devin_api_token.get_secret_value()}",
                "Accept": "application/json",
            },
        )

    def __repr__(self) -> str:
        # Written out rather than defaulted, because a default repr of a future subclass holding
        # the token as an attribute would print it. The two fields here identify the deployment.
        return (
            f"{type(self).__name__}(base_url={self._settings.devin_api_base!r}, "
            f"org_id={self._settings.devin_org_id!r})"
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Sessions -------------------------------------------------------------------------------

    async def create_session(
        self,
        *,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        issue_class: str | IssueClass,
        delivery_id: str,
        repo: str | None = None,
        base_branch: str | None = None,
        knowledge_ids: Sequence[str] | None = None,
    ) -> Session:
        """The remediation session for one issue: **adopted if it already exists, created if not.**

        Takes the issue rather than a request body: every field of that body is derived from
        `playbooks.py` and the configuration, and the two that a caller could get wrong — the tag
        set and the ACU cap — are the ones a reviewer checks in the Devin dashboard.

        Idempotent, and it has to be, because a `POST` that times out has still been received: on
        2026-08-11 nine sessions existed for three issues, all of them Sentinel's, because the
        retries of a request that had already been served each made another one. `find_session`
        below is the whole of the answer — it runs first, and a session already tagged for this
        remediation is returned instead of a second one being made. That covers the *job* being
        retried by `pipeline/worker.py` minutes later exactly as it covers a retry within one call,
        which a narrower retry policy would not.

        The create itself is sent under `SEND_ONCE`. Everything it could have retried — a `429`, a
        `5xx`, a timeout — is handed back to the queue, which comes back through this method and
        adopts whatever the previous attempt turned out to have made.

        Raises `UnknownIssueClass` for a class with no playbook and `MissingPlaybookId` for one
        whose id was never configured, both before anything is sent. Raises rather than creating if
        the lookup itself cannot be completed.
        """
        repo = self._settings.target_repo if repo is None else repo
        base_branch = self._settings.target_base_branch if base_branch is None else base_branch
        configured = self._settings.devin_knowledge_ids
        request = CreateSessionRequest(
            prompt=initial_prompt(
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                repo=repo,
                base_branch=base_branch,
            ),
            title=session_title(issue_number, issue_title),
            tags=tuple(
                session_tags(
                    repo=repo,
                    issue_number=issue_number,
                    issue_class=issue_class,
                    delivery_id=delivery_id,
                )
            ),
            repos=(repo,),
            playbook_id=playbook_id_for(issue_class, self._settings.devin_playbook_ids),
            knowledge_ids=tuple(configured if knowledge_ids is None else knowledge_ids),
            max_acu_limit=acu_cap_for(issue_class),
        )

        adopted = await self.find_session(repo=repo, issue_number=issue_number)
        if adopted is not None:
            return adopted

        started = time.perf_counter()
        payload = await self._request(
            "POST",
            SESSIONS,
            json=request.model_dump(mode="json"),
            path=self._org(),
            retry=SEND_ONCE,
        )
        session = self._parse(Session, payload, "POST", SESSIONS)
        log.info(
            "devin.session.created",
            session_id=session.session_id,
            issue=issue_number,
            duration_ms=_millis(time.perf_counter() - started),
        )
        return session

    async def find_session(self, *, repo: str, issue_number: int) -> Session | None:
        """The session this remediation already has, if one was created for it — otherwise `None`.

        `session_identity` is the key: the three tags every session of this remediation carries and
        no session of another one does. They are sent as the listing's `tags` filter *and* checked
        again on every session that comes back, because the reference documents
        `SessionsQueryParams.tags` as an array without saying whether the server ANDs them, ORs
        them, or ignores an unregistered one. Whichever it does, only a session carrying all three
        is adopted; the filter is an optimisation and the check is the rule.

        The cursor is followed for the same reason. Answering "there is no session" from the first
        page of a listing that is not filtering would create a duplicate, which is the one outcome
        this method exists to prevent — so **only** `has_next_page: false` ends the walk with a
        negative answer. A page that claims another page and gives no cursor to reach it is a
        disagreement, not a terminator: it raises, because answering `None` there would be reading
        the prefix as the whole.

        Where several match — the nine sessions of 2026-08-11 were three sets of three — one is
        chosen deterministically, so repeated attempts converge on the same session rather than
        each adopting a different one: a session still live in preference to an archived one, then
        the earliest created, then the lowest id. The earliest is the one whose response was lost,
        and the one with the most work already done; the later ones are what the retries made.

        The adopted session is returned **as it is**, not reconciled against the request that was
        built for it. An issue relabelled between the lost `POST` and this call is running under the
        playbook and `class:` tag of the original — narrow, and preferable to a second session, but
        it is the one way a remediation can be attached to a session it would not have created
        today.
        """
        identity = session_identity(repo=repo, issue_number=issue_number)
        wanted = frozenset(identity)
        matches: list[Session] = []
        seen = 0
        cursor: str | None = None
        for page_number in range(1, LOOKUP_PAGE_LIMIT + 1):
            page = await self._session_page(tags=identity, first=LOOKUP_PAGE_SIZE, after=cursor)
            seen += len(page.sessions)
            matches.extend(
                session for session in page.sessions if wanted <= frozenset(session.tags)
            )
            if not page.has_next_page:
                return self._adopt(matches, issue_number, pages=page_number, seen=seen)
            if page.end_cursor is None:
                raise SessionLookupIncomplete(
                    f"GET {SESSIONS} reported another page and no end_cursor to reach it, on page "
                    f"{page_number} for issue {issue_number}; nothing was created by this call"
                )
            cursor = page.end_cursor
        # The budget is spent, so nothing here can say there is no session — but what the walk did
        # find is not in doubt. Every match passed the tag check on this side, which is the same
        # evidence an ordinary answer rests on, so adopting one is safe and is strictly better than
        # failing a remediation while a session of its own runs unrecorded.
        if matches:
            return self._adopt(matches, issue_number, pages=LOOKUP_PAGE_LIMIT, seen=seen)
        raise SessionLookupIncomplete(
            f"GET {SESSIONS} still reported more pages after {LOOKUP_PAGE_LIMIT} of "
            f"{LOOKUP_PAGE_SIZE} for issue {issue_number}; {seen} sessions were seen and none of "
            f"them was this remediation's, which is not the same as there being none. Nothing was "
            f"created by this call"
        )

    def _adopt(
        self, matches: list[Session], issue_number: int, *, pages: int, seen: int
    ) -> Session | None:
        """The chosen session, or `None` — and a log line either way.

        The negative answer is logged as deliberately as the positive one. It is the branch that
        goes on to create a session, so when a duplicate next appears it is the branch an operator
        has to be able to see: what was filtered on, how far the walk got, and how much it looked
        at. A `seen` of zero against an organisation known to have sessions is what a `tags`
        parameter the server misparses looks like, and nothing else would say so.
        """
        if not matches:
            log.info("devin.session.absent", issue=issue_number, pages=pages, seen=seen)
            return None
        chosen = min(matches, key=_adoption_order)
        log.info(
            "devin.session.adopted",
            session_id=chosen.session_id,
            issue=issue_number,
            is_archived=chosen.is_archived,
            matched=len(matches),
            pages=pages,
            seen=seen,
        )
        return chosen

    async def get_session(self, session_id: str) -> Session:
        """One session, as the poller reconciles it: status, `status_detail`, ACUs, pull requests
        and the structured report."""
        payload = await self._request("GET", SESSION, path=self._org(session_id=session_id))
        return self._parse(Session, payload, "GET", SESSION)

    async def list_sessions(
        self, *, tags: Sequence[str] = (), limit: int | None = None
    ) -> tuple[Session, ...]:
        """Sessions of this organisation, for backfill and in-flight listing.

        `tags` filters server-side where the organisation supports it; every session Sentinel
        creates carries the `sentinel` namespace tag, which is what makes ours findable among an
        organisation's own. Filters are checked against the registered vocabulary rather than the
        session rule: a search may name a `class:` value Sentinel would never create a session with.

        `tags` is a **repeated** query parameter — `SessionsQueryParams.tags` is an array of
        strings — not the comma-joined string this sent, which would have asked for one tag whose
        name contains a comma and matched nothing. The page size is `first` (default 100, maximum
        200), not `limit`, which the reference does not define.

        There is no pagination here. `after` is the documented cursor and the body observed on
        2026-08-10 carries `end_cursor`, `has_next_page` and `total` beside the items — so the
        endpoint does paginate and this reads the first page only. A backfill over an organisation
        with more sessions than one page silently sees a prefix of them, which is worth closing.
        `find_session` does follow the cursor, because a prefix is not an answer to the question it
        asks.
        """
        return (await self._session_page(tags=tags, first=limit)).sessions

    async def _session_page(
        self, *, tags: Sequence[str] = (), first: int | None = None, after: str | None = None
    ) -> SessionPage:
        """One page of the session listing, cursor and all."""
        params: dict[str, Any] = {}
        if tags:
            params["tags"] = [registered_tag(tag) for tag in tags]
        if first is not None:
            params["first"] = first
        if after is not None:
            params["after"] = after
        payload = await self._request("GET", SESSIONS, params=params, path=self._org())
        return self._parse(SessionPage, payload, "GET", SESSIONS)

    async def send_message(self, session_id: str, message: str) -> None:
        """Feed a resumable session the next fact — a CI failure, a review requesting changes.

        The message is built by `playbooks.ci_failure_message` or `changes_requested_message`,
        which state the new fact and restate the goal without prescribing the fix.
        """
        await self._request(
            "POST",
            SESSION_MESSAGES,
            json={"message": message},
            path=self._org(session_id=session_id),
        )

    async def tag_session(self, session_id: str, tags: Sequence[str]) -> None:
        """Append lifecycle tags — `cycle:2`, `outcome:merged`.

        Every tag is checked against the registered vocabulary before the request is made, so an
        unregistered one raises `UnregisteredTag` here instead of `422` at Devin (B7).
        """
        await self._request(
            "POST",
            SESSION_TAGS,
            json={"tags": [validate_tag(tag) for tag in tags]},
            path=self._org(session_id=session_id),
        )

    # --- Bootstrap ------------------------------------------------------------------------------

    async def register_tags(self, tags: Sequence[str]) -> None:
        """Register the organisation's allowed tag vocabulary, once, at bootstrap.

        Not validated against `validate_tag`: what is registered is the vocabulary itself — the
        namespace tag and the prefixes of `playbooks.TAG_PREFIXES` — and a prefix on its own is not
        a tag that would pass validation.

        A `PUT`, and therefore a **replacement** rather than an addition: the v3 reference calls
        this "Replace the full set of allowed session tags for an organization" and documents a
        separate `POST` that appends. Re-running it is harmless, which is the idempotence
        `scripts/bootstrap_devin.py` claims for it; the *first* run is what can take away tags
        nobody here chose to remove. `list_tags` is how `--dry-run` names them beforehand.
        """
        await self._request("PUT", ORGANIZATION_TAGS, json={"tags": list(tags)}, path=self._org())

    async def list_tags(self) -> Degradable[TagVocabulary]:
        """The organisation's allowed session tags, as they are before a registration replaces them.

        Read-only, and read for one caller: `scripts/bootstrap_devin.py --dry-run`, which cannot
        say what the registration would take away without it.

        Degradable rather than raising, with more reason than the others: the documented permission
        is `ManageEnterpriseSettings`, which a service user scoped to one organisation may well not
        carry, and `ALLOWED_TAGS` is not the path the registration writes to. A refusal is an
        ordinary outcome here rather than a fault, and the preview's answer to it — that the `PUT`
        may remove tags it cannot name — is worth more than a run that stops.
        """
        return await self._degradable(
            Capability.TAG_DISCOVERY, "GET", ALLOWED_TAGS, TagVocabulary, path=self._org()
        )

    async def create_knowledge_note(self, *, name: str, body: str, trigger: str) -> KnowledgeNote:
        """Seed one repository convention, so every session starts with it instead of rediscovering
        it. The returned id becomes an entry of `DEVIN_KNOWLEDGE_IDS`.

        `KnowledgeNoteCreateRequest` requires all three: `name`, `body` and **`trigger`** — when
        Devin should reach for the note. It was sent as an optional `trigger_description`, which
        the reference does not define, so every note would have been rejected `422` for the field
        that was missing rather than the one that was extra.
        """
        payload = await self._request(
            "POST",
            KNOWLEDGE_NOTES,
            json={"name": name, "body": body, "trigger": trigger},
            path=self._org(),
        )
        return self._parse(KnowledgeNote, payload, "POST", KNOWLEDGE_NOTES)

    # --- Degradable ------------------------------------------------------------------------------

    async def list_playbooks(self) -> Degradable[PlaybookPage]:
        """The organisation's playbooks, titled and identified — what `DEVIN_PLAYBOOK_IDS` is
        built from.

        The four playbooks were created by hand in the Devin UI, so their ids exist only there
        until something reads them back. This is that read, and it works against the live API — the
        organisation scope carries writes as well, which is what makes B6's premise wrong; creating
        them by script has simply not been tried.
        `scripts/bootstrap_devin.py --list-playbooks` is the only caller and it creates nothing.

        Degradable rather than raising, for the reason the two below are: a refused permission is
        answered by reading the ids off the playbook pages in the web app, not by failing.

        One page. The endpoint takes `after` and `first` (default 100) and neither is sent, so an
        organisation with more playbooks than one page reports `has_next_page` and the rest are
        found in the web app — a cursor loop for a one-off lookup of four ids is machinery nothing
        else in Sentinel would use.
        """
        return await self._degradable(
            Capability.PLAYBOOK_DISCOVERY, "GET", PLAYBOOKS, PlaybookPage, path=self._org()
        )

    async def daily_consumption(self) -> Degradable[Consumption]:
        """Daily ACU spend, for the budget guard and the cost panel.

        Degrades to `Unavailable` when the organisation does not expose consumption; the caller
        sums `acus_consumed` across sessions instead.

        The endpoint takes optional `time_before` and `time_after` epochs and neither is sent, so
        what comes back is whatever window Devin considers current. What the reference does **not**
        say is what that default window is, and `acus_on(today)` is only the day's spend if it
        includes today — so this remains the one ambiguity here that fails *silently*, and it is
        worth confirming against a real response before the guard is trusted to stop work.

        The day boundary is Devin's, not ours: "Billing cycles use midnight PST (Pacific Standard
        Time) as the day boundary". A guard comparing a UTC `today` against a Pacific billing day
        is reading the right number for a day that is up to eight hours out of step with its own.
        """
        return await self._degradable(
            Capability.ACU_SPEND, "GET", CONSUMPTION_DAILY, Consumption, path=self._org()
        )

    async def session_metrics(
        self, *, time_after: int, time_before: int
    ) -> Degradable[SessionMetrics]:
        """Merged-PR and ACU aggregates from Devin's own accounting — enterprise scope, optional.

        The window is the caller's, and it is not optional: `time_before` and `time_after` are the
        only two query parameters the reference marks `required: true`, in epoch seconds. Sending
        neither — which is what this did — is a `422`, and a `422` is not one of the refusals
        `DEGRADES` turns into a fallback, so the capability would have raised out of every caller
        rather than degrading. There is no window this client could pick on the caller's behalf
        without inventing the very figure the panel is meant to report, so the caller states one.

        Not attempted at all without `DEVIN_ENTERPRISE_ID`: the deployment has said it does not
        claim enterprise scope, and a request that is certain to be refused is not worth a round
        trip or a `403` in the logs on every dashboard refresh.
        """
        if self._settings.devin_enterprise_id is None:
            return self._unavailable(Capability.SESSION_METRICS, Unavailability.NOT_CONFIGURED)
        return await self._degradable(
            Capability.SESSION_METRICS,
            "GET",
            ENTERPRISE_SESSION_METRICS,
            SessionMetrics,
            params={"time_after": time_after, "time_before": time_before},
        )

    # --- Internals -------------------------------------------------------------------------------

    def _org(self, **path: str) -> PathParams:
        """The path parameters of an organisation-scoped endpoint, plus whatever else it takes."""
        return {"org_id": self._settings.devin_org_id, **path}

    async def _degradable[M: BaseModel](
        self,
        capability: Capability,
        method: str,
        endpoint: str,
        model: type[M],
        path: PathParams | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Degradable[M]:
        """One optional capability: parsed, or the reason it could not be served.

        A body that does not parse degrades here, where a session that does not parse raises. The
        asymmetry is deliberate. These two endpoints are the ones whose field names the spec does
        not give and no credentials exist to check against (B8), so the likeliest way for them to
        fail is a name we guessed wrong — and the spec's answer to "this capability is not
        available" is a labelled fallback, not a dashboard that errors. A session is the opposite:
        there is no fallback for one, and a report that does not parse must escalate.
        """
        try:
            payload = await self._request(method, endpoint, path=path, params=params)
        except DevinAPIError as exc:
            reason = DEGRADES.get(exc.status_code)
            if reason is None:
                raise
            return self._unavailable(capability, reason, exc.status_code)
        try:
            return Available(self._parse(model, payload, method, endpoint))
        except DevinResponseError as exc:
            return self._unavailable(capability, Unavailability.UNREADABLE, detail=str(exc))

    def _unavailable(
        self,
        capability: Capability,
        reason: Unavailability,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> Unavailable:
        result = Unavailable(capability=capability, reason=reason, status_code=status_code)
        log.warning(
            "devin.capability.unavailable",
            capability=capability.value,
            reason=reason.value,
            status=status_code,
            detail=detail,
            fallback=result.fallback,
        )
        return result

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        path: PathParams | None = None,
        retry: RetryPolicy | None = None,
    ) -> Any:
        """One call, with the retry policy applied. Returns the decoded body, or `None` if empty.

        `endpoint` is the template; `path` fills it. Both the metric label and every message built
        here use the template, so no session id reaches a Prometheus label or an exception.

        `retry` overrides the client's policy for this one call. It exists for `SEND_ONCE`: whether
        a request may be repeated is a property of what the request *does*, not of the deployment,
        and the create is the one route on a *remediation's* path where repeating it creates
        something. `create_knowledge_note` is the other repeat-unsafe `POST` here; it runs only
        from `make bootstrap-devin`, whose idempotence is `.env`'s job
        (`docs/adr/2026-08-08-env-is-the-bootstrap-scripts-record.md`), and it is left alone.
        """
        policy = self._retry if retry is None else retry
        url = endpoint.format(**(path or {}))
        waited = 0.0
        for attempt in range(1, policy.attempts + 1):
            started = time.perf_counter()
            try:
                response = await self._client.request(
                    method, url, json=json, params=dict(params) if params else None
                )
            except httpx.TransportError as exc:
                elapsed = time.perf_counter() - started
                self._observe(method, endpoint, DevinOutcome.NETWORK_ERROR, elapsed)
                # The class name, not the exception: a connection error renders the URL, and a
                # proxy URL can carry credentials.
                error: DevinError = DevinTransportError(
                    f"{method} {endpoint}: {type(exc).__name__}"
                )
                retry_after: float | None = None
                status: int | None = None
            else:
                elapsed = time.perf_counter() - started
                status = response.status_code
                self._observe(method, endpoint, DevinOutcome.from_status(status), elapsed)
                log.debug(
                    "devin.request",
                    method=method,
                    endpoint=endpoint,
                    status=status,
                    duration_ms=_millis(elapsed),
                    attempt=attempt,
                )
                if response.is_success:
                    return _decode(method, endpoint, response)
                error = DevinAPIError(
                    method=method, endpoint=endpoint, status_code=status, body=response.text
                )
                retry_after = retry_after_seconds(response)

            if not error.retryable or attempt == policy.attempts:
                log.warning(
                    "devin.request.failed",
                    method=method,
                    endpoint=endpoint,
                    status=status,
                    attempts=attempt,
                )
                raise error

            delay = self._delay(policy, attempt, retry_after, waited)
            waited += delay
            log.warning(
                "devin.request.retry",
                method=method,
                endpoint=endpoint,
                status=status,
                attempt=attempt,
                delay_ms=_millis(delay),
            )
            await self._sleep(delay)
        raise AssertionError("the retry loop returns or raises on its last attempt")

    def _delay(
        self, policy: RetryPolicy, attempt: int, retry_after: float | None, waited: float
    ) -> float:
        """How long to wait before the next attempt, given how long this call has waited already.

        `Retry-After` wins when Devin sent one — it knows when the window resets and we do not —
        capped, so a header we misread cannot stall a worker indefinitely. Otherwise the delay is
        jittered across the top half of the exponential ceiling: enough spread to break up a burst
        of workers retrying together, without collapsing the backoff to nearly nothing.

        Whichever it is, it is then clamped to what is left of `max_total_delay`, so the wall-clock
        cost of one call is bounded by the policy rather than by the arithmetic happening to work
        out at the current attempt count.
        """
        ceiling = policy.ceiling(attempt)
        if retry_after is not None:
            delay = min(retry_after, policy.max_delay)
        else:
            delay = self._rng.uniform(ceiling / 2, ceiling)
        return max(0.0, min(delay, policy.max_total_delay - waited))

    def _observe(self, method: str, endpoint: str, outcome: DevinOutcome, elapsed: float) -> None:
        self._metrics.observe_devin_request(
            method=method, endpoint=endpoint, outcome=outcome, seconds=elapsed
        )

    def _parse[M: BaseModel](self, model: type[M], payload: Any, method: str, endpoint: str) -> M:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            # Locations and messages only. The payload itself is not repeated into the exception:
            # pydantic's own rendering includes the input it rejected, which is the whole body.
            faults = "; ".join(
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in exc.errors(include_url=False)
            )
            raise DevinResponseError(
                f"{method} {endpoint} returned a body {model.__name__} does not accept — {faults}"
            ) from None


def _adoption_order(session: Session) -> tuple[bool, bool, int, str]:
    """Which of several sessions for one remediation is adopted — `min` of this key wins.

    A live session before an archived one, because archiving is how a human stops a session and the
    one they left running is the one they meant to keep. Then the earliest, which is the session the
    lost response belonged to and the one with the most work already done. Then the id, so the order
    is total and two workers racing the same lookup cannot disagree.

    A session whose body carried no `created_at` sorts **after** every session that did: not knowing
    when a session started is not evidence that it started first, and the reference marks the field
    required, so its absence says something is wrong with that body rather than something early
    about that session.

    Note what is *not* here: no branch that creates a session when every match is archived. A human
    archiving all of them is exactly the 2026-08-11 incident being cleaned up by hand, and answering
    that with a tenth session would be the defect volunteering for a rematch. The remediation adopts
    an archived session, the poller reports it as stopped, and it escalates — visibly, and without
    spending anything.
    """
    return (
        session.is_archived,
        session.created_at is None,
        session.created_at or 0,
        session.session_id,
    )


def _decode(method: str, endpoint: str, response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        raise DevinResponseError(f"{method} {endpoint} returned a body that is not JSON") from None


def _millis(seconds: float) -> int:
    return round(seconds * 1000)
