"""The GitHub REST client — everything the pipeline asks of GitHub, and nothing that merges.

The side effects in `docs/04-state-machine.md` are the whole requirement: read the issue a
remediation started from, comment on it, label it, close it, read the pull request, read the check
runs on a head SHA, fetch the log of the job that failed, read the inline comments of a review that
requested changes, and request a review. `ROUTES` below is the complete list of endpoints this
process can reach.

**There is no merge.** `docs/adr/2026-08-07-humans-approve-every-merge.md` is a property of the
system, not a convention, so it is expressed as one: every path this client can build comes from
`ROUTES`, and `tests/test_github_client.py` asserts that no entry is `PUT /repos/…/pulls/…/merge`
and that no method reaches it. Adding the ability to merge would mean adding a route, which fails
that test.

Two behaviours are the client's own rather than the caller's:

- **Rate limiting.** GitHub has two of them, and a bot that comments and labels meets the secondary
  one first. `403` with `x-ratelimit-remaining: 0`, `403` carrying `retry-after`, and `429` are all
  waited out and retried, whatever the method: the request was rejected before it was processed, so
  repeating it cannot repeat a side effect. A `5xx` is retried only for an idempotent method,
  because GitHub answering `502` *after* creating a comment is exactly how a duplicate escalation
  appears. Everything else in the `4xx` range fails immediately, because retrying a validation
  error only spends quota (`docs/06-event-pipeline.md#reliability-policy`). The total wait inside
  one call is bounded — see
  `docs/adr/2026-08-08-github-waits-are-bounded-and-handed-back-to-the-queue.md`.
- **Redaction.** The token is never a URL parameter, never logged, and scrubbed out of every
  exception message this module raises, because a `GitHubError` is recorded in
  `remediation_event.detail` and reaches the dashboard from there — a path that never passes through
  the `structlog` redactor in `sentinel.observability.logging`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Final, Self
from urllib.parse import quote

import httpx

from sentinel.config import Settings, get_settings
from sentinel.devin.playbooks import LOG_EXCERPT_LINES
from sentinel.observability.logging import REDACTED, get_logger

log = get_logger(__name__)

GITHUB_API_BASE: Final = "https://api.github.com"
API_VERSION: Final = "2022-11-28"
"""`X-GitHub-Api-Version`. Pinned: an unversioned request follows whatever the default becomes."""

PAGE_SIZE: Final = 100

RETRY_ATTEMPTS: Final = 3
BACKOFF_BASE_SECONDS: Final = 1.0
MAX_WAIT_SECONDS: Final = 60.0
"""The longest this client spends waiting in one call, summed over every retry it makes. A primary
rate limit resets up to an hour later, and a worker that slept through it would lose its job lease
(`JOB_LEASE_TIMEOUT`, 15 minutes) and have the work reclaimed underneath it. Once this budget is
spent the delay GitHub named is handed back as `GitHubRateLimited.retry_after` for the queue to
schedule."""

REQUEST_TIMEOUT_SECONDS: Final = 30.0

IDEMPOTENT_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "DELETE", "PUT"})
"""The methods a `5xx` may be retried on. A `502` can be returned after the write succeeded, so
repeating a `POST` — a comment, a label, a review request — can duplicate it. `POST` and `PATCH`
are handed to the queue instead, where the attempt is recorded on the remediation."""

ROUTES: Final[Mapping[str, str]] = {
    "issue": "/repos/{repo}/issues/{number}",
    "issue_comments": "/repos/{repo}/issues/{number}/comments",
    "issue_labels": "/repos/{repo}/issues/{number}/labels",
    "issue_label": "/repos/{repo}/issues/{number}/labels/{label}",
    "pull_request": "/repos/{repo}/pulls/{number}",
    "review_comments": "/repos/{repo}/pulls/{number}/reviews/{review_id}/comments",
    "requested_reviewers": "/repos/{repo}/pulls/{number}/requested_reviewers",
    "check_runs": "/repos/{repo}/commits/{sha}/check-runs",
    "workflow_runs": "/repos/{repo}/actions/runs",
    "workflow_run_jobs": "/repos/{repo}/actions/runs/{run_id}/jobs",
    "job_logs": "/repos/{repo}/actions/jobs/{job_id}/logs",
}
"""Every endpoint this client can address. The single source of the paths, so that what Sentinel is
able to do to a repository can be read off one table rather than reconstructed from ten methods."""

FAILING_CONCLUSIONS: Final[frozenset[str]] = frozenset({"failure", "timed_out", "startup_failure"})
"""Which workflow runs and jobs `get_failing_job` will select from. `docs/04-state-machine.md`
names the first two as the conclusions that drive `CI_FAILED`; `startup_failure` is a job that
never ran, whose log says why, and it is included here because a job that failed to start still has
a cause worth forwarding. `cancelled`, `neutral` and `skipped` are deliberately absent — the spec
maps them to no transition.

This is the vocabulary for *choosing a log to fetch*, and it is deliberately wider than the check
suite conclusions that drive the state machine. Which conclusions cause a transition is decided
from the webhook by `sentinel.github.events`, not here."""

LOG_EXPIRED: Final[frozenset[int]] = frozenset({404, 410})
"""GitHub expires Actions logs. Losing the log is not losing the failure — see `get_failing_job`."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class GitHubError(RuntimeError):
    """A request to GitHub that did not succeed, and will not be retried by this client.

    `status_code` is `0` when there was no response at all — a timeout, a dropped connection.
    """

    def __init__(self, *, method: str, path: str, status_code: int, message: str) -> None:
        failed = f"failed with {status_code}" if status_code else "failed"
        super().__init__(f"{method} {path} {failed}: {message}")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.message = message


class GitHubNotFound(GitHubError):
    """`404` — the issue, label or pull request is not there."""


class GitHubUnavailable(GitHubError):
    """GitHub could not be reached, or kept failing, for the whole of this call.

    Retryable at the job level: the queue's own backoff is the outer loop
    (`docs/06-event-pipeline.md#reliability-policy`).
    """


class GitHubRateLimited(GitHubUnavailable):
    """Rate limited for longer than one call may wait.

    `retry_after` is how many seconds GitHub asked for, so the caller can schedule rather than
    guess. It is `None` when GitHub did not say.
    """

    def __init__(self, *, retry_after: float | None, **fields: Any) -> None:
        super().__init__(**fields)
        self.retry_after = retry_after


# --- What the pipeline reads -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Issue:
    """The originating issue of a remediation, as `docs/04-state-machine.md` needs it."""

    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    html_url: str
    author: str | None


@dataclass(frozen=True, slots=True)
class IssueComment:
    """A comment Sentinel posted. Returned so an escalation can be linked to from the logs."""

    id: int
    html_url: str
    body: str


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    title: str
    body: str
    state: str
    draft: bool
    merged: bool
    merged_at: datetime | None
    head_sha: str
    head_ref: str
    base_ref: str
    html_url: str


@dataclass(frozen=True, slots=True)
class ReviewComment:
    """One inline comment a reviewer left on a line of the diff.

    The `pull_request_review` webhook carries the review's body and nothing of these, so the
    `CHANGES_REQUESTED -> RUNNING` edge has to fetch them (`docs/04-state-machine.md`). They are
    returned as data rather than as text: rendering them into the resume message is the caller's,
    which is what `changes_requested_message(inline_comments=…)` documents.
    """

    id: int
    path: str
    line: int | None
    body: str
    author: str | None
    html_url: str


@dataclass(frozen=True, slots=True)
class CheckRun:
    """One check run on a head SHA.

    There is deliberately no `failed` property, though these objects are now what the CI verdict is
    read from. Whether a *pull request* should move to `CI_FAILED` is not a property of any single
    run — it depends on which run this is, and on what every other run on the SHA is doing — so
    `sentinel.github.checks.verdict` answers it over the whole collection and nothing here offers a
    per-run shortcut that would be mistaken for the same question.
    """

    id: int
    name: str
    status: str
    conclusion: str | None
    html_url: str | None
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class FailingJob:
    """The job whose failure the Devin session is resumed with.

    `log_excerpt` is already the tail — `LOG_EXCERPT_LINES` lines of it — because this is what goes
    into the resume message (`sentinel.devin.playbooks.ci_failure_message`), and a whole Superset CI
    log is megabytes that would crowd out the instruction following it. It is empty when the log
    could not be fetched, which `ci_failure_message` renders as `NO_LOG_OUTPUT`.
    """

    id: int
    name: str
    html_url: str
    log_excerpt: str


def _text(value: Any) -> str:
    """A string field GitHub is entitled to return as `null` — an issue filed with a title alone."""
    return value if isinstance(value, str) else ""


def _time(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _login(value: Any) -> str | None:
    """The account behind an object, or `None` — a deleted account is returned as `null`."""
    if not isinstance(value, dict):
        return None
    login: str = value["login"]
    return login


def _as_issue(payload: Mapping[str, Any]) -> Issue:
    return Issue(
        number=payload["number"],
        title=payload["title"],
        body=_text(payload.get("body")),
        state=payload["state"],
        labels=tuple(label["name"] for label in payload.get("labels", ())),
        html_url=payload["html_url"],
        author=_login(payload.get("user")),
    )


def _as_pull_request(payload: Mapping[str, Any]) -> PullRequest:
    return PullRequest(
        number=payload["number"],
        title=payload["title"],
        body=_text(payload.get("body")),
        state=payload["state"],
        draft=bool(payload.get("draft")),
        merged=bool(payload.get("merged")),
        merged_at=_time(payload.get("merged_at")),
        head_sha=payload["head"]["sha"],
        head_ref=payload["head"]["ref"],
        base_ref=payload["base"]["ref"],
        html_url=payload["html_url"],
    )


def _as_check_run(payload: Mapping[str, Any]) -> CheckRun:
    return CheckRun(
        id=payload["id"],
        name=payload["name"],
        status=payload["status"],
        conclusion=payload.get("conclusion"),
        html_url=payload.get("html_url"),
        started_at=_time(payload.get("started_at")),
        completed_at=_time(payload.get("completed_at")),
    )


def _as_review_comment(payload: Mapping[str, Any]) -> ReviewComment:
    return ReviewComment(
        id=payload["id"],
        path=payload["path"],
        # `line` is null on a comment against a line that later changed — the review is still worth
        # forwarding, so the position is dropped rather than the comment.
        line=payload.get("line"),
        body=_text(payload.get("body")),
        author=_login(payload.get("user")),
        html_url=payload["html_url"],
    )


def _failed(payload: Mapping[str, Any]) -> bool:
    return payload.get("conclusion") in FAILING_CONCLUSIONS


def _earliest_first(job: Mapping[str, Any]) -> tuple[bool, datetime, int]:
    """Sort key for `min` over jobs: the one that started first, ties broken on id.

    A job GitHub has not stamped with a `started_at` sorts **last**, not first. Sorting it to the
    epoch would make every unstamped job beat a real failure and become the whole of the evidence
    the resumed session receives — and `startup_failure`, a job that never ran, is exactly the
    population likely to carry one.
    """
    when = _time(job.get("started_at"))
    return (when is None, when or _EPOCH, job["id"])


def _newest(run: Mapping[str, Any]) -> int:
    """Sort key for `max` over workflow runs: the newest attempt on this SHA.

    The id, not a timestamp. GitHub allocates run ids in increasing order, so the highest is the
    most recently created — and unlike `run_started_at` it is never absent, so there is no missing
    value to rank. A run queued later can also *start* earlier than one ahead of it in the queue,
    which would have the runner pool decide which attempt counts as current.
    """
    identifier: int = run["id"]
    return identifier


def _excerpt(text: str) -> str:
    return "\n".join(text.strip().splitlines()[-LOG_EXCERPT_LINES:])


# --- The client ------------------------------------------------------------------------------


class GitHubClient:
    """An async client for `TARGET_REPO`, holding one connection pool.

        async with GitHubClient(settings) as github:
            issue = await github.get_issue(42876)
            await github.comment_on_issue(42876, "…")

    One instance per process is the intended usage; it is safe to share between tasks. `sleep` is
    injected so the retry policy can be asserted without a test waiting out a real backoff.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        settings = get_settings() if settings is None else settings
        self._repo = settings.target_repo
        self._token = settings.github_token.get_secret_value()
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            timeout=REQUEST_TIMEOUT_SECONDS,
            # A job log is served as a redirect to a signed blob URL on another host. httpx drops
            # the Authorization header when a redirect crosses origins, which is both what keeps
            # the token off that host and why the signed URL needs no credential of its own.
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "sentinel",
            },
        )

    def __repr__(self) -> str:
        # The default repr of an object holding a configured `httpx.AsyncClient` is harmless today,
        # but the token is one attribute away from it and a repr reaches logs and tracebacks.
        return f"GitHubClient(repo={self._repo!r})"

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
        await self._http.aclose()

    # --- Issues ---

    async def get_issue(self, number: int) -> Issue:
        response = await self._request("GET", self._url("issue", number=number))
        return _as_issue(_object(response))

    async def comment_on_issue(self, number: int, body: str) -> IssueComment:
        """Comment on an issue — or on a pull request, which GitHub numbers as one.

        The escalation comment on `BLOCKED` and `FAILED`, and the root-cause summary posted on the
        pull request when CI goes green, are both this call.
        """
        response = await self._request(
            "POST", self._url("issue_comments", number=number), json={"body": body}
        )
        payload = _object(response)
        return IssueComment(
            id=payload["id"], html_url=payload["html_url"], body=_text(payload.get("body"))
        )

    async def add_labels(self, number: int, labels: Sequence[str]) -> tuple[str, ...]:
        """Add labels to an issue, leaving the ones already on it alone. Returns the full set."""
        if not labels:
            raise ValueError("add_labels needs at least one label")
        response = await self._request(
            "POST", self._url("issue_labels", number=number), json={"labels": list(labels)}
        )
        return tuple(label["name"] for label in _array(response))

    async def remove_label(self, number: int, label: str) -> bool:
        """Remove one label, reporting whether it was there.

        GitHub answers `404` for a label the issue does not carry, which is the ordinary outcome of
        removing `devin:autofix` twice — a redelivered webhook, a retried job. It is returned as
        `False` rather than raised so that the caller's retry is idempotent, and returned rather
        than swallowed so that "it was already gone" stays visible.

        An issue that does not exist answers `404` too, and this reports `False` for it as well.
        Telling them apart means matching on the text of `message`, which is not part of the API
        contract; a remediation whose issue has been deleted has a larger problem than a label.
        """
        path = self._url("issue_label", number=number, label=quote(label, safe=""))
        try:
            await self._request("DELETE", path)
        except GitHubNotFound:
            return False
        return True

    async def close_issue(self, number: int) -> Issue:
        """Close the issue. The `IN_REVIEW -> MERGED` side effect in `docs/04-state-machine.md`."""
        response = await self._request(
            "PATCH", self._url("issue", number=number), json={"state": "closed"}
        )
        return _as_issue(_object(response))

    # --- Pull requests ---

    async def get_pull_request(self, number: int) -> PullRequest:
        response = await self._request("GET", self._url("pull_request", number=number))
        return _as_pull_request(_object(response))

    async def get_review_comments(self, number: int, review_id: int) -> tuple[ReviewComment, ...]:
        """The inline comments of one review, in the order the reviewer left them.

        Scoped to the review rather than to the pull request: `CHANGES_REQUESTED -> RUNNING` is
        driven by one `pull_request_review.submitted` delivery, and forwarding every inline comment
        the pull request has ever collected would re-send the previous lap's, which the session has
        already acted on.

        The review's own body is not fetched — it arrives on the webhook. These do not.
        """
        comments = await self._paginate(
            self._url("review_comments", number=number, review_id=review_id), key=None
        )
        return tuple(_as_review_comment(comment) for comment in comments)

    async def request_review(
        self,
        number: int,
        *,
        reviewers: Sequence[str] = (),
        team_reviewers: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """Ask for a human review. Returns the reviewers now requested on the pull request.

        Empty on both sides is refused here rather than sent: GitHub answers `422`, which this
        client does not retry, so the remediation would fail on a caller's mistake.
        """
        if not reviewers and not team_reviewers:
            raise ValueError("request_review needs at least one reviewer or team")
        body: dict[str, Any] = {}
        if reviewers:
            body["reviewers"] = list(reviewers)
        if team_reviewers:
            body["team_reviewers"] = list(team_reviewers)
        response = await self._request(
            "POST", self._url("requested_reviewers", number=number), json=body
        )
        payload = _object(response)
        return tuple(user["login"] for user in payload.get("requested_reviewers", ()))

    # --- CI ---

    async def get_check_runs(self, sha: str) -> tuple[CheckRun, ...]:
        """Every check run on a head SHA, whichever app produced it."""
        runs = await self._paginate(self._url("check_runs", sha=sha), "check_runs")
        return tuple(_as_check_run(run) for run in runs)

    async def get_failing_job(self, sha: str, *, workflow_path: str) -> FailingJob | None:
        """The failing job of the latest failing run of `workflow_path` on `sha`, with its log tail.

        `None` when that workflow did not fail on this SHA — it was cancelled, it has not run, or it
        failed with every job cancelled. It is an answer, not an error: `ci_failure_message` has
        `NO_LOG_OUTPUT` for exactly this.

        **`workflow_path` is not optional, and the reason is a defect.** This used to select over
        every workflow run on the SHA. `taxpon/superset` carries 46 of them, one of which —
        `Dependency Review` — fails on every pull request there because the repository has no
        dependency graph enabled. On the first live remediation that run was the newest failure, so
        its log became the excerpt a Devin session was resumed with: a two-file frontend diff, and a
        message asking it to fix a repository setting. A resume message must quote the check that
        judged the diff, so this asks for that workflow by path and reports nothing rather than
        substituting somebody else's failure
        ([ADR](../../../docs/adr/2026-08-10-ci-green-is-the-aggregate-of-the-check-runs.md)).

        `sentinel.github.checks.verdict` only reports `FAILED` when this same workflow's gate job
        failed, so in the case that reaches here the run is present and the narrowing finds it.

        Which of that run's jobs is picked is the older decision, and unchanged; see
        `docs/adr/2026-08-08-the-ci-excerpt-comes-from-the-earliest-failing-job.md`.
        """
        runs = await self._paginate(
            self._url("workflow_runs"), "workflow_runs", params={"head_sha": sha}
        )
        failed_runs = [run for run in runs if run.get("path") == workflow_path and _failed(run)]
        if not failed_runs:
            return None
        run = max(failed_runs, key=_newest)

        jobs = await self._paginate(self._url("workflow_run_jobs", run_id=run["id"]), "jobs")
        failing = [job for job in jobs if _failed(job)]
        if not failing:
            return None
        job = min(failing, key=_earliest_first)

        return FailingJob(
            id=job["id"],
            name=job["name"],
            html_url=job["html_url"],
            log_excerpt=await self._job_log(job["id"]),
        )

    async def _job_log(self, job_id: int) -> str:
        """The tail of one job's log, or nothing if GitHub no longer has it.

        Actions logs expire, and the answer is then `410 Gone` — a `4xx`, so neither this client
        nor the queue retries it and the `resume_session` job would fail the remediation over a log
        that was merely too old. The failure itself is not lost: the job's name and URL still
        describe it, and `ci_failure_message` renders an empty excerpt as `NO_LOG_OUTPUT`.
        """
        try:
            response = await self._request("GET", self._url("job_logs", job_id=job_id))
        except GitHubError as exc:
            if exc.status_code not in LOG_EXPIRED:
                raise
            log.warning("github.job_log.expired", job_id=job_id, status=exc.status_code)
            return ""
        return _excerpt(response.text)

    # --- HTTP ---

    def _url(self, route: str, **values: object) -> str:
        return ROUTES[route].format(repo=self._repo, **values)

    def _redact(self, text: str) -> str:
        return text.replace(self._token, REDACTED)

    async def _paginate(
        self, path: str, key: str | None = None, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Every item of a paginated collection, following `Link: rel="next"`.

        `key` names the field holding the items, for the endpoints that wrap them in an envelope
        alongside `total_count`; the ones that answer with a bare array pass `None`.

        Following the header rather than stopping at the first page: one head SHA should produce a
        single check suite on the fork
        (`docs/adr/2026-08-08-one-check-suite-on-the-fork.md`), but a page boundary that silently
        truncated the check runs would look exactly like CI having produced nothing.
        """
        items: list[dict[str, Any]] = []
        url: str | None = path
        query: Mapping[str, Any] | None = {**(params or {}), "per_page": PAGE_SIZE}
        while url is not None:
            response = await self._request("GET", url, params=query)
            items += _array(response) if key is None else _object(response)[key]
            following = response.links.get("next")
            # The next URL carries the cursor and the page size already.
            url, query = (following["url"], None) if following else (None, None)
        return items

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """One GitHub call, with the retry policy applied. Raises `GitHubError` on anything else.

        `waited` is the whole of the waiting budget, not the size of one wait: two waits of 60
        seconds are two minutes of a 15-minute job lease spent inside a single call, which is not
        what `MAX_WAIT_SECONDS` claims to bound.
        """
        attempt = 0
        waited = 0.0
        while True:
            attempt += 1
            last = attempt == RETRY_ATTEMPTS
            try:
                response = await self._http.request(method, url, json=json, params=params)
            except httpx.HTTPError as exc:
                # `exc` holds the request, and the request holds the Authorization header. Only the
                # redacted text of the message is kept, and the cause is dropped with it.
                reason = self._redact(f"{type(exc).__name__}: {exc}")
                log.warning(
                    "github.request.error", method=method, path=url, attempt=attempt, reason=reason
                )
                wait = _backoff(attempt)
                if last or waited + wait > MAX_WAIT_SECONDS:
                    raise GitHubUnavailable(
                        method=method, path=url, status_code=0, message=reason
                    ) from None
                waited += wait
                await self._sleep(wait)
                continue

            if response.is_success:
                return response

            reason = self._describe(response)
            log.warning(
                "github.request.failed",
                method=method,
                path=url,
                status=response.status_code,
                attempt=attempt,
                reason=reason,
            )
            if not _retryable(response, method):
                raise self._failure(method, url, response, reason)

            requested = _retry_after(response)
            wait = _backoff(attempt) if requested is None else requested
            if last or waited + wait > MAX_WAIT_SECONDS:
                raise self._exhausted(method, url, response, reason, requested)
            waited += wait
            await self._sleep(wait)

    def _describe(self, response: httpx.Response) -> str:
        """What went wrong, in the words of the API — recorded on the remediation, so redacted."""
        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        message = payload.get("message") if isinstance(payload, dict) else None
        return self._redact(message if isinstance(message, str) else response.reason_phrase)

    def _failure(
        self, method: str, path: str, response: httpx.Response, reason: str
    ) -> GitHubError:
        """A response this client will not repeat, as the type that says whether the queue should.

        `GitHubUnavailable` means try again later; `GitHubError` and `GitHubNotFound` mean the
        request was wrong and will stay wrong. A `5xx` on a `POST` reaches here rather than being
        retried in place, and it is still the queue's to retry — under a job attempt that is
        recorded, rather than silently inside this call.
        """
        status = response.status_code
        fields: dict[str, Any] = {
            "method": method,
            "path": path,
            "status_code": status,
            "message": reason,
        }
        if status >= httpx.codes.INTERNAL_SERVER_ERROR:
            return GitHubUnavailable(**fields)
        if status == httpx.codes.NOT_FOUND:
            return GitHubNotFound(**fields)
        return GitHubError(**fields)

    def _exhausted(
        self, method: str, path: str, response: httpx.Response, reason: str, requested: float | None
    ) -> GitHubUnavailable:
        """Retryable, but not within this call — the queue schedules the next attempt.

        `requested` is what GitHub named and nothing else. Passing the backoff this client would
        have used instead would hand the queue an invented delay wearing GitHub's authority, and
        leave it unable to tell a real answer from a guess.
        """
        fields: dict[str, Any] = {
            "method": method,
            "path": path,
            "status_code": response.status_code,
            "message": reason,
        }
        if _rate_limited(response):
            return GitHubRateLimited(retry_after=requested, **fields)
        return GitHubUnavailable(**fields)


def _decoded(response: httpx.Response) -> Any:
    """The JSON body, or a `GitHubError` naming the request that produced something else.

    A `200` carrying HTML is what an interception proxy or a captive portal answers with. Left
    alone it surfaces as a `JSONDecodeError` from outside this module's exception hierarchy, and
    the worker records a remediation failure that names neither the endpoint nor the status.
    """
    try:
        return response.json()
    except ValueError:
        raise GitHubError(
            method=response.request.method,
            path=response.request.url.path,
            status_code=response.status_code,
            message=f"expected JSON, got {response.headers.get('content-type', 'no content type')}",
        ) from None


def _object(response: httpx.Response) -> dict[str, Any]:
    payload: dict[str, Any] = _decoded(response)
    return payload


def _array(response: httpx.Response) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = _decoded(response)
    return payload


def _rate_limited(response: httpx.Response) -> bool:
    """Both of GitHub's rate limits.

    The primary one answers `403` with `x-ratelimit-remaining: 0`; the secondary one answers `403`
    or `429` with `retry-after`. A plain `403` is neither — it is a token without the scope for
    this call, and retrying it changes nothing.
    """
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        return True
    if response.status_code != httpx.codes.FORBIDDEN:
        return False
    headers = response.headers
    return headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers


def _retryable(response: httpx.Response, method: str) -> bool:
    """Whether repeating this request is both useful and safe.

    A rate limit is safe on any method — the request was rejected before it was processed. A `5xx`
    is not: GitHub can answer `502` after the write landed, so retrying a `POST` is how one
    escalation becomes two comments on the issue. The queue retries those instead, where the second
    attempt is a row on the remediation rather than something invisible inside one call.
    """
    if _rate_limited(response):
        return True
    return (
        response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
        and method.upper() in IDEMPOTENT_METHODS
    )


def _backoff(attempt: int) -> float:
    """Exponential, and without jitter: these retries are in-process and few, and the queue's own
    backoff — which does jitter — is the outer loop for anything that outlives one call."""
    return BACKOFF_BASE_SECONDS * 2.0 ** (attempt - 1)


def _retry_after(response: httpx.Response) -> float | None:
    """The delay GitHub named, from either of the two headers it names one in."""
    header = response.headers.get("retry-after")
    if header is not None:
        try:
            # Documented as a number of seconds. GitHub does not send the HTTP-date form here, and
            # a value that is not a number is treated as no answer rather than as zero.
            return max(0.0, float(header))
        except ValueError:
            return None
    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = response.headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                return max(0.0, float(reset) - time.time())
            except ValueError:
                return None
    return None
