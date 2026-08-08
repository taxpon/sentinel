"""The GitHub client: what it sends, what it makes of what comes back, and what it cannot do.

Three things here are not ordinary coverage.

**The request body is the assertion.** `.claude/rules/testing.md` requires it, and the reason is
specific to this client: a comment posted to the wrong issue, or a label added to the wrong
repository, is a request that succeeds. Only the captured body distinguishes it from the right one,
so every write method is asserted on `sent.json` and on the full path, never on the fact that a
call happened.

**No method can merge.** `docs/adr/2026-08-07-humans-approve-every-merge.md` is a property of the
system, so it is tested as one, twice over: the route table cannot express a merge, and every
public method — enumerated from the class, so a new one cannot slip past — is driven against a fake
that would record the attempt.

**The token cannot leak.** It is asserted absent from the log stream, from the exception text, from
the `repr`, from every URL, and from the request that follows the log redirect off `api.github.com`.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from conftest import GITHUB_API_BASE, GITHUB_TOKEN, Configure, FakeAPI
from factories import ISSUE_NUMBER, PR_NUMBER, REPO, github_payload
from sentinel.config import Settings
from sentinel.devin.playbooks import LOG_EXCERPT_LINES
from sentinel.github.client import (
    MAX_WAIT_SECONDS,
    RETRY_ATTEMPTS,
    ROUTES,
    GitHubClient,
    GitHubError,
    GitHubNotFound,
    GitHubRateLimited,
    GitHubUnavailable,
)

HEAD_SHA = "349a7f639dfb353669c001187706d7fd0112ed2f"
AUTOFIX_LABEL = "devin:autofix"
NEEDS_HUMAN = "needs-human"

ISSUES = f"/repos/{REPO}/issues"
PULLS = f"/repos/{REPO}/pulls"
ACTIONS = f"/repos/{REPO}/actions"

RUN_ID = 84410727864
JOB_ID = 231855142299


# ------------------------------------------------------------------------------------- fixtures


@pytest.fixture
def waits() -> list[float]:
    """Every backoff the client took, in order, instead of real time passing."""
    return []


@pytest.fixture
async def github(
    settings: Settings, github_api: FakeAPI, waits: list[float]
) -> AsyncIterator[GitHubClient]:
    """A client on the faked API. `github_api` is requested so `respx` is patched in first."""

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    async with GitHubClient(settings, sleep=sleep) as client:
        yield client


def a_check_run(**overrides: Any) -> dict[str, Any]:
    return {
        "id": 51602138455,
        "name": "pytest",
        "status": "completed",
        "conclusion": "failure",
        "html_url": f"https://github.com/{REPO}/runs/51602138455",
        "started_at": "2026-08-07T15:12:04Z",
        "completed_at": "2026-08-07T15:19:41Z",
        **overrides,
    }


def a_workflow_run(**overrides: Any) -> dict[str, Any]:
    return {
        "id": RUN_ID,
        "name": "devin-autofix-ci",
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "failure",
        "run_started_at": "2026-08-07T15:11:55Z",
        **overrides,
    }


def a_job(**overrides: Any) -> dict[str, Any]:
    return {
        "id": JOB_ID,
        "run_id": RUN_ID,
        "name": "pytest",
        "status": "completed",
        "conclusion": "failure",
        "started_at": "2026-08-07T15:12:04Z",
        "html_url": f"https://github.com/{REPO}/actions/runs/{RUN_ID}/job/{JOB_ID}",
        **overrides,
    }


# --------------------------------------------------------------------------------- reading


async def test_get_issue_parses_the_recorded_payload(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    recorded = github_payload("issues.labeled")["issue"]
    github_api.responds("GET", f"{ISSUES}/{ISSUE_NUMBER}", json=recorded)

    issue = await github.get_issue(ISSUE_NUMBER)

    assert github_api.only("GET").path == f"{ISSUES}/{ISSUE_NUMBER}"
    assert issue.number == ISSUE_NUMBER
    assert issue.title == "Helm chart: database credentials cannot be loaded from existing secret"
    assert issue.state == "open"
    assert issue.author == "detjensrobert"
    assert issue.html_url == f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}"
    assert AUTOFIX_LABEL in issue.labels
    assert issue.body.startswith("### Bug description")


async def test_an_issue_filed_with_no_description_reads_as_empty_not_none(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    # GitHub returns `"body": null`, and `initial_prompt` calls `.strip()` on whatever it is given.
    recorded = {**github_payload("issues.labeled")["issue"], "body": None, "user": None}
    github_api.responds("GET", f"{ISSUES}/{ISSUE_NUMBER}", json=recorded)

    issue = await github.get_issue(ISSUE_NUMBER)

    assert issue.body == ""
    assert issue.author is None


async def test_get_pull_request_parses_the_recorded_payload(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    recorded = github_payload("pull_request.opened")["pull_request"]
    github_api.responds("GET", f"{PULLS}/{PR_NUMBER}", json=recorded)

    pull_request = await github.get_pull_request(PR_NUMBER)

    assert github_api.only("GET").path == f"{PULLS}/{PR_NUMBER}"
    assert pull_request.number == PR_NUMBER
    assert pull_request.state == "open"
    assert pull_request.head_sha == HEAD_SHA
    assert pull_request.head_ref == "fix-adhoc-column-error-mislabel"
    assert pull_request.base_ref == "master"
    assert pull_request.draft is False
    assert pull_request.merged is False
    assert pull_request.merged_at is None


async def test_a_merged_pull_request_carries_the_merge_time(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    recorded = github_payload("pull_request.closed.merged")["pull_request"]
    github_api.responds("GET", f"{PULLS}/{PR_NUMBER}", json=recorded)

    pull_request = await github.get_pull_request(PR_NUMBER)

    assert pull_request.merged is True
    assert pull_request.state == "closed"
    assert pull_request.merged_at == datetime(2026, 8, 7, 22, 3, 50, tzinfo=UTC)


# --------------------------------------------------------------------------------- writing


async def test_comment_on_issue_sends_the_body_to_that_issue(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    recorded = github_payload("issue_comment.created")["comment"]
    github_api.responds("POST", f"{ISSUES}/{ISSUE_NUMBER}/comments", 201, json=recorded)

    comment = await github.comment_on_issue(ISSUE_NUMBER, "Blocked: the fix needs a human.")

    sent = github_api.only("POST")
    # The path is asserted in full: a comment on the right issue of the wrong repository, or on the
    # pull request number rather than the issue number, is a request that succeeds.
    assert sent.path == f"{ISSUES}/{ISSUE_NUMBER}/comments"
    assert sent.json == {"body": "Blocked: the fix needs a human."}
    assert comment.id == recorded["id"]
    assert comment.html_url == recorded["html_url"]


async def test_add_labels_sends_the_labels_and_returns_the_resulting_set(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    github_api.responds(
        "POST",
        f"{ISSUES}/{ISSUE_NUMBER}/labels",
        json=[{"name": AUTOFIX_LABEL}, {"name": NEEDS_HUMAN}],
    )

    labels = await github.add_labels(ISSUE_NUMBER, [NEEDS_HUMAN])

    sent = github_api.only("POST")
    assert sent.path == f"{ISSUES}/{ISSUE_NUMBER}/labels"
    assert sent.json == {"labels": [NEEDS_HUMAN]}
    assert labels == (AUTOFIX_LABEL, NEEDS_HUMAN)


async def test_add_labels_refuses_an_empty_list_without_calling_github(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    with pytest.raises(ValueError, match="at least one label"):
        await github.add_labels(ISSUE_NUMBER, [])

    assert github_api.requests == []


async def test_remove_label_escapes_the_label_into_the_path(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    github_api.responds("DELETE", f"{ISSUES}/{ISSUE_NUMBER}/labels/devin%3Aautofix", json=[])

    removed = await github.remove_label(ISSUE_NUMBER, AUTOFIX_LABEL)

    sent = github_api.only("DELETE")
    # `devin:autofix` unescaped would make the colon a path delimiter question rather than a name.
    assert sent.url.raw_path.decode() == f"{ISSUES}/{ISSUE_NUMBER}/labels/devin%3Aautofix"
    assert removed is True


async def test_removing_a_label_that_is_not_there_reports_false_and_does_not_retry(
    github: GitHubClient, github_api: FakeAPI, waits: list[float]
) -> None:
    # The ordinary outcome of a redelivered `unlabeled` webhook, not a fault.
    github_api.responds(
        "DELETE", f"{ISSUES}/{ISSUE_NUMBER}/labels/devin%3Aautofix", 404, json={"message": "n/a"}
    )

    removed = await github.remove_label(ISSUE_NUMBER, AUTOFIX_LABEL)

    assert removed is False
    assert len(github_api.sent("DELETE")) == 1
    assert waits == []


async def test_close_issue_sends_the_closed_state(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    recorded = {**github_payload("issues.labeled")["issue"], "state": "closed"}
    github_api.responds("PATCH", f"{ISSUES}/{ISSUE_NUMBER}", json=recorded)

    issue = await github.close_issue(ISSUE_NUMBER)

    sent = github_api.only("PATCH")
    assert sent.path == f"{ISSUES}/{ISSUE_NUMBER}"
    assert sent.json == {"state": "closed"}
    assert issue.state == "closed"


async def test_request_review_sends_the_reviewers_it_was_given(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    github_api.responds(
        "POST",
        f"{PULLS}/{PR_NUMBER}/requested_reviewers",
        201,
        json={"number": PR_NUMBER, "requested_reviewers": [{"login": "taxpon"}]},
    )

    requested = await github.request_review(PR_NUMBER, reviewers=["taxpon"])

    sent = github_api.only("POST")
    assert sent.path == f"{PULLS}/{PR_NUMBER}/requested_reviewers"
    assert sent.json == {"reviewers": ["taxpon"]}
    assert requested == ("taxpon",)


async def test_request_review_sends_teams_separately_from_people(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    github_api.responds(
        "POST", f"{PULLS}/{PR_NUMBER}/requested_reviewers", 201, json={"requested_reviewers": []}
    )

    await github.request_review(PR_NUMBER, reviewers=["taxpon"], team_reviewers=["platform"])

    # Two lists, not one: GitHub rejects a team login in `reviewers` with a 422 this client does
    # not retry.
    assert github_api.only("POST").json == {
        "reviewers": ["taxpon"],
        "team_reviewers": ["platform"],
    }


async def test_request_review_refuses_an_empty_request_without_calling_github(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    with pytest.raises(ValueError, match="at least one reviewer"):
        await github.request_review(PR_NUMBER)

    assert github_api.requests == []


# ------------------------------------------------------------------------------------- CI


async def test_get_check_runs_parses_the_runs_on_a_head_sha(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    github_api.responds(
        "GET",
        f"/repos/{REPO}/commits/{HEAD_SHA}/check-runs",
        json={
            "total_count": 2,
            "check_runs": [
                a_check_run(),
                a_check_run(id=51602138999, name="jest", conclusion="success"),
            ],
        },
    )

    runs = await github.get_check_runs(HEAD_SHA)

    sent = github_api.only("GET")
    assert sent.path == f"/repos/{REPO}/commits/{HEAD_SHA}/check-runs"
    assert sent.url.params["per_page"] == "100"
    assert [(run.name, run.conclusion, run.failed) for run in runs] == [
        ("pytest", "failure", True),
        ("jest", "success", False),
    ]
    assert runs[0].started_at == datetime(2026, 8, 7, 15, 12, 4, tzinfo=UTC)


async def test_check_runs_beyond_the_first_page_are_followed(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    # A truncated collection looks exactly like CI having produced nothing, so the `Link` header is
    # followed rather than the first page being taken as the whole of it.
    path = f"/repos/{REPO}/commits/{HEAD_SHA}/check-runs"
    second = f"{GITHUB_API_BASE}{path}?per_page=100&page=2"
    github_api.route("GET", path).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"total_count": 2, "check_runs": [a_check_run(name="pre-commit")]},
                headers={"Link": f'<{second}>; rel="next"'},
            ),
            httpx.Response(200, json={"total_count": 2, "check_runs": [a_check_run(name="jest")]}),
        ]
    )

    runs = await github.get_check_runs(HEAD_SHA)

    assert [run.name for run in runs] == ["pre-commit", "jest"]
    assert str(github_api.sent("GET")[1].url) == second


async def test_get_failing_job_returns_the_log_tail_of_the_failing_job(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    log = "\n".join(f"line {n}" for n in range(1, 151))
    fake_ci(github_api, jobs=[a_job()], log=log)

    job = await github.get_failing_job(HEAD_SHA)

    assert job is not None
    assert job.id == JOB_ID
    assert job.name == "pytest"
    assert job.html_url == f"https://github.com/{REPO}/actions/runs/{RUN_ID}/job/{JOB_ID}"
    # The tail, at the length `playbooks.LOG_EXCERPT_LINES` sets — the failure and its traceback
    # are at the end, and this excerpt is the whole of what the resumed session is told.
    assert job.log_excerpt.splitlines() == [
        f"line {n}" for n in range(151 - LOG_EXCERPT_LINES, 151)
    ]
    assert len(job.log_excerpt.splitlines()) == LOG_EXCERPT_LINES


async def test_the_workflow_runs_are_filtered_to_the_head_sha(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    fake_ci(github_api, jobs=[a_job()])

    await github.get_failing_job(HEAD_SHA)

    listed = github_api.sent("GET", f"{ACTIONS}/runs")[0]
    assert listed.url.params["head_sha"] == HEAD_SHA


async def test_get_failing_job_takes_the_earliest_failing_job_not_the_aggregate(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    # `devin-autofix-ci` fails whenever a signal job does, and its log says only that. It starts
    # last because it depends on them, which is what makes "earliest" the right rule.
    aggregate = a_job(
        id=JOB_ID + 3,
        name="devin-autofix-ci",
        started_at="2026-08-07T15:20:00Z",
    )
    passed = a_job(
        id=JOB_ID - 1, name="scope", conclusion="success", started_at="2026-08-07T15:11:59Z"
    )
    fake_ci(github_api, jobs=[aggregate, passed, a_job()])

    job = await github.get_failing_job(HEAD_SHA)

    assert job is not None
    assert job.name == "pytest"
    assert github_api.only("GET", f"{ACTIONS}/jobs/{JOB_ID}/logs").path.endswith(f"{JOB_ID}/logs")


async def test_get_failing_job_takes_the_latest_run_when_a_sha_was_re_run(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    superseded = a_workflow_run(id=RUN_ID - 10, run_started_at="2026-08-07T14:00:00Z")
    latest = a_workflow_run()
    fake_ci(github_api, runs=[superseded, latest], jobs=[a_job()])

    await github.get_failing_job(HEAD_SHA)

    assert github_api.only("GET", f"{ACTIONS}/runs/{RUN_ID}/jobs")


@pytest.mark.parametrize(
    ("runs", "jobs"),
    [
        ([a_workflow_run(conclusion="success")], [a_job()]),
        ([a_workflow_run()], [a_job(conclusion="cancelled")]),
        ([], []),
    ],
    ids=["no-failing-run", "no-failing-job", "no-run-at-all"],
)
async def test_get_failing_job_is_none_when_nothing_failed(
    github: GitHubClient,
    github_api: FakeAPI,
    runs: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
) -> None:
    # An answer rather than an error: `ci_failure_message` has `NO_LOG_OUTPUT` for this.
    fake_ci(github_api, runs=runs, jobs=jobs)

    assert await github.get_failing_job(HEAD_SHA) is None
    assert github_api.sent("GET", f"{ACTIONS}/jobs/{JOB_ID}/logs") == []


async def test_the_log_redirect_off_github_does_not_carry_the_token(
    github: GitHubClient, github_api: FakeAPI, http_mock: respx.MockRouter
) -> None:
    # GitHub answers the logs endpoint with a redirect to a signed blob URL on another host. The
    # signature is the credential there; ours must not travel with it.
    blob = "https://productionresultssa.blob.core.windows.net/actions-results/pytest.txt"
    fake_ci(github_api, jobs=[a_job()])
    github_api.responds("GET", f"{ACTIONS}/jobs/{JOB_ID}/logs", 302, headers={"Location": blob})
    blob_route = http_mock.get(blob).mock(
        return_value=httpx.Response(200, text="AssertionError: boom")
    )

    job = await github.get_failing_job(HEAD_SHA)

    assert job is not None
    assert job.log_excerpt == "AssertionError: boom"
    followed = blob_route.calls.last.request
    assert "authorization" not in followed.headers
    assert GITHUB_TOKEN not in str(followed.headers)


# ------------------------------------------------------------------------- rate limits and retries


def rate_limited(**headers: str) -> httpx.Response:
    """A primary rate limit: `403` with the remaining budget at zero."""
    return httpx.Response(
        403,
        json={"message": "API rate limit exceeded"},
        headers={"x-ratelimit-remaining": "0", **headers},
    )


async def test_a_secondary_rate_limit_waits_for_the_time_it_asked_for(
    github: GitHubClient, github_api: FakeAPI, waits: list[float]
) -> None:
    # The limit a bot that comments and labels meets first. `retry-after` is GitHub's own answer,
    # so it is used in preference to the backoff schedule.
    github_api.route("POST", f"{ISSUES}/{ISSUE_NUMBER}/comments").mock(
        side_effect=[
            httpx.Response(429, json={"message": "too fast"}, headers={"retry-after": "7"}),
            httpx.Response(201, json=github_payload("issue_comment.created")["comment"]),
        ]
    )

    await github.comment_on_issue(ISSUE_NUMBER, "escalating")

    assert waits == [7.0]
    assert len(github_api.sent("POST")) == 2
    # The retry carries the same body — a comment must not be truncated or reordered by a retry.
    assert [sent.json for sent in github_api.sent("POST")] == [{"body": "escalating"}] * 2


async def test_a_primary_rate_limit_waits_until_the_budget_resets(
    github: GitHubClient, github_api: FakeAPI, waits: list[float]
) -> None:
    reset = time.time() + 5
    github_api.route("GET", f"{ISSUES}/{ISSUE_NUMBER}").mock(
        side_effect=[
            rate_limited(**{"x-ratelimit-reset": str(reset)}),
            httpx.Response(200, json=github_payload("issues.labeled")["issue"]),
        ]
    )

    issue = await github.get_issue(ISSUE_NUMBER)

    assert issue.number == ISSUE_NUMBER
    assert waits == [pytest.approx(5, abs=1.0)]


async def test_a_rate_limit_that_names_no_delay_backs_off_exponentially(
    github: GitHubClient, github_api: FakeAPI, waits: list[float]
) -> None:
    github_api.route("GET", f"{ISSUES}/{ISSUE_NUMBER}").mock(
        side_effect=[
            httpx.Response(429, json={"message": "too fast"}),
            httpx.Response(429, json={"message": "too fast"}),
            httpx.Response(200, json=github_payload("issues.labeled")["issue"]),
        ]
    )

    await github.get_issue(ISSUE_NUMBER)

    assert waits == [1.0, 2.0]


@pytest.mark.parametrize(
    "headers",
    [
        {"x-ratelimit-remaining": "0"},
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "soon"},
        {"x-ratelimit-remaining": "0", "retry-after": "Wed, 08 Aug 2026 17:00:00 GMT"},
    ],
    ids=["no-reset", "unparseable-reset", "http-date-retry-after"],
)
async def test_a_rate_limit_header_it_cannot_read_falls_back_to_the_schedule(
    github: GitHubClient, github_api: FakeAPI, waits: list[float], headers: dict[str, str]
) -> None:
    # An intermediary can rewrite these, and a delay that cannot be read is still a rate limit.
    # Treating it as "no answer" backs off; treating it as zero would hammer the limit.
    github_api.route("GET", f"{ISSUES}/{ISSUE_NUMBER}").mock(
        side_effect=[
            httpx.Response(403, json={"message": "rate limited"}, headers=headers),
            httpx.Response(200, json=github_payload("issues.labeled")["issue"]),
        ]
    )

    await github.get_issue(ISSUE_NUMBER)

    assert waits == [1.0]


async def test_a_5xx_is_retried(
    github: GitHubClient, github_api: FakeAPI, waits: list[float]
) -> None:
    github_api.route("GET", f"{ISSUES}/{ISSUE_NUMBER}").mock(
        side_effect=[
            httpx.Response(502, text="<html>bad gateway</html>"),
            httpx.Response(200, json=github_payload("issues.labeled")["issue"]),
        ]
    )

    issue = await github.get_issue(ISSUE_NUMBER)

    assert issue.number == ISSUE_NUMBER
    assert waits == [1.0]


@pytest.mark.parametrize("status", [400, 401, 404, 409, 410, 422])
async def test_a_4xx_fails_immediately(
    github: GitHubClient, github_api: FakeAPI, waits: list[float], status: int
) -> None:
    # Retrying a validation error only spends quota — `docs/06-event-pipeline.md`.
    github_api.responds("GET", f"{ISSUES}/{ISSUE_NUMBER}", status, json={"message": "no"})

    with pytest.raises(GitHubError) as raised:
        await github.get_issue(ISSUE_NUMBER)

    assert raised.value.status_code == status
    assert raised.value.message == "no"
    assert len(github_api.sent("GET")) == 1
    assert waits == []


async def test_a_403_that_is_not_a_rate_limit_fails_immediately(
    github: GitHubClient, github_api: FakeAPI, waits: list[float]
) -> None:
    # A token without the scope for this call. Retrying it changes nothing, so only the two
    # rate-limit shapes are treated as retryable.
    github_api.responds(
        "GET",
        f"{ISSUES}/{ISSUE_NUMBER}",
        403,
        json={"message": "Resource not accessible by personal access token"},
        headers={"x-ratelimit-remaining": "4987"},
    )

    with pytest.raises(GitHubError) as raised:
        await github.get_issue(ISSUE_NUMBER)

    assert not isinstance(raised.value, GitHubRateLimited)
    assert len(github_api.sent("GET")) == 1
    assert waits == []


async def test_a_404_is_raised_as_its_own_type(github: GitHubClient, github_api: FakeAPI) -> None:
    github_api.responds("GET", f"{PULLS}/{PR_NUMBER}", 404, json={"message": "Not Found"})

    with pytest.raises(GitHubNotFound):
        await github.get_pull_request(PR_NUMBER)


async def test_a_wait_longer_than_the_bound_is_handed_back_to_the_caller(
    github: GitHubClient, github_api: FakeAPI, waits: list[float]
) -> None:
    # A primary limit resets up to an hour later. Sleeping through it inside a claimed job would
    # lose the lease and have the work reclaimed — see the ADR on bounded waits.
    github_api.responds(
        "GET",
        f"{ISSUES}/{ISSUE_NUMBER}",
        403,
        json={"message": "API rate limit exceeded"},
        headers={"x-ratelimit-remaining": "0", "retry-after": "3600"},
    )

    with pytest.raises(GitHubRateLimited) as raised:
        await github.get_issue(ISSUE_NUMBER)

    assert raised.value.retry_after == 3600.0
    assert raised.value.retry_after > MAX_WAIT_SECONDS
    assert waits == []
    assert len(github_api.sent("GET")) == 1


async def test_a_rate_limit_that_outlasts_the_attempts_is_raised_with_what_it_asked_for(
    github: GitHubClient, github_api: FakeAPI, waits: list[float]
) -> None:
    github_api.responds(
        "GET",
        f"{ISSUES}/{ISSUE_NUMBER}",
        429,
        json={"message": "slow down"},
        headers={"retry-after": "3"},
    )

    with pytest.raises(GitHubRateLimited) as raised:
        await github.get_issue(ISSUE_NUMBER)

    assert raised.value.retry_after == 3.0
    assert len(github_api.sent("GET")) == RETRY_ATTEMPTS
    assert waits == [3.0] * (RETRY_ATTEMPTS - 1)


async def test_a_network_error_is_retried_and_then_reported_as_unavailable(
    github: GitHubClient, github_api: FakeAPI, waits: list[float]
) -> None:
    github_api.route("GET", f"{ISSUES}/{ISSUE_NUMBER}").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(GitHubUnavailable) as raised:
        await github.get_issue(ISSUE_NUMBER)

    assert not isinstance(raised.value, GitHubRateLimited)
    assert raised.value.status_code == 0
    assert "ConnectError" in raised.value.message
    assert len(github_api.sent("GET")) == RETRY_ATTEMPTS
    assert waits == [1.0, 2.0]


# ------------------------------------------------------------------------------ it cannot merge


EXPECTED_ROUTES = {
    "issue",
    "issue_comments",
    "issue_labels",
    "issue_label",
    "pull_request",
    "requested_reviewers",
    "check_runs",
    "workflow_runs",
    "workflow_run_jobs",
    "job_logs",
}

PUBLIC_METHODS = frozenset(
    name
    for name, value in vars(GitHubClient).items()
    if callable(value) and not name.startswith("_") and name != "aclose"
)


def test_the_route_table_is_the_whole_of_what_this_client_can_reach() -> None:
    # Pinned rather than derived: reaching a new endpoint should be a decision someone made here,
    # and the test below is only as strong as this set is complete.
    assert set(ROUTES) == EXPECTED_ROUTES


def test_no_route_can_merge_a_pull_request() -> None:
    # `docs/adr/2026-08-07-humans-approve-every-merge.md`. Merging is `PUT /repos/{o}/{r}/pulls/
    # {n}/merge`; there is no other endpoint that merges, so its absence from the table is the
    # property, stated as an assertion.
    assert [route for route, path in ROUTES.items() if "merge" in path] == []


async def test_no_method_reaches_the_merge_endpoint(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    """Drive every public method and inspect what actually went out.

    The route table is a claim about the code; this is the observation. `PUBLIC_METHODS` is read
    off the class, so a method added without a line here fails on the last assertion rather than
    going unexercised.
    """
    answer_everything(github_api)

    await github.get_issue(ISSUE_NUMBER)
    await github.comment_on_issue(ISSUE_NUMBER, "summary")
    await github.add_labels(ISSUE_NUMBER, [NEEDS_HUMAN])
    await github.remove_label(ISSUE_NUMBER, AUTOFIX_LABEL)
    await github.close_issue(ISSUE_NUMBER)
    await github.get_pull_request(PR_NUMBER)
    await github.request_review(PR_NUMBER, reviewers=["taxpon"])
    await github.get_check_runs(HEAD_SHA)
    await github.get_failing_job(HEAD_SHA)
    driven = {
        "get_issue",
        "comment_on_issue",
        "add_labels",
        "remove_label",
        "close_issue",
        "get_pull_request",
        "request_review",
        "get_check_runs",
        "get_failing_job",
    }

    assert driven == PUBLIC_METHODS, "a public method exists that this test does not exercise"
    assert [sent.path for sent in github_api.requests if sent.path.endswith("/merge")] == []
    assert [sent.method for sent in github_api.requests if sent.method == "PUT"] == []


# --------------------------------------------------------------------------- the token stays put


async def test_the_token_is_a_header_and_never_a_url_parameter(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    answer_everything(github_api)

    await github.get_issue(ISSUE_NUMBER)
    await github.get_check_runs(HEAD_SHA)
    await github.get_failing_job(HEAD_SHA)

    for sent in github_api.requests:
        assert sent.headers["authorization"] == f"Bearer {GITHUB_TOKEN}"
        assert sent.headers["x-github-api-version"] == "2022-11-28"
        assert GITHUB_TOKEN not in str(sent.url)


async def test_an_error_message_echoing_the_token_is_redacted(
    github: GitHubClient, github_api: FakeAPI
) -> None:
    # A `GitHubError` is recorded in `remediation_event.detail` and rendered on the dashboard, a
    # path that never passes through the `structlog` redactor. So the message is scrubbed here.
    github_api.responds(
        "GET",
        f"{ISSUES}/{ISSUE_NUMBER}",
        422,
        json={"message": f"Bad credentials: {GITHUB_TOKEN}"},
    )

    with pytest.raises(GitHubError) as raised:
        await github.get_issue(ISSUE_NUMBER)

    assert GITHUB_TOKEN not in str(raised.value)
    assert GITHUB_TOKEN not in raised.value.message
    assert "[redacted]" in raised.value.message


async def test_nothing_the_client_logs_carries_the_token(
    settings: Settings, github_api: FakeAPI, capture: Configure
) -> None:
    logs = capture()
    github_api.responds("GET", f"{ISSUES}/{ISSUE_NUMBER}", 404, json={"message": "Not Found"})

    async def sleep(seconds: float) -> None:
        raise AssertionError("a 404 must not be retried")

    async with GitHubClient(settings, sleep=sleep) as client:
        with pytest.raises(GitHubNotFound):
            await client.get_issue(ISSUE_NUMBER)

    assert logs.records, "the failure was not logged at all"
    assert GITHUB_TOKEN not in logs.text
    assert logs.last["event"] == "github.request.failed"
    assert logs.last["status"] == 404


def test_the_repr_does_not_carry_the_token(settings: Settings) -> None:
    client = GitHubClient(settings)

    assert GITHUB_TOKEN not in repr(client)
    assert REPO in repr(client)


# ------------------------------------------------------------------------------------- helpers


def fake_ci(
    github_api: FakeAPI,
    *,
    runs: list[dict[str, Any]] | None = None,
    jobs: list[dict[str, Any]] | None = None,
    log: str = "AssertionError: boom",
) -> None:
    """A head SHA with a failed workflow run behind it."""
    runs = [a_workflow_run()] if runs is None else runs
    jobs = [] if jobs is None else jobs
    github_api.responds(
        "GET", f"{ACTIONS}/runs", json={"total_count": len(runs), "workflow_runs": runs}
    )
    for run in runs:
        github_api.responds(
            "GET", f"{ACTIONS}/runs/{run['id']}/jobs", json={"total_count": len(jobs), "jobs": jobs}
        )
    for job in jobs:
        github_api.responds("GET", f"{ACTIONS}/jobs/{job['id']}/logs", text=log)


def answer_everything(github_api: FakeAPI) -> None:
    """Every route answered, so that a test about what was *sent* is not about what came back."""
    issue = github_payload("issues.labeled")["issue"]
    github_api.responds("GET", f"{ISSUES}/{ISSUE_NUMBER}", json=issue)
    github_api.responds("PATCH", f"{ISSUES}/{ISSUE_NUMBER}", json=issue)
    github_api.responds(
        "POST",
        f"{ISSUES}/{ISSUE_NUMBER}/comments",
        201,
        json=github_payload("issue_comment.created")["comment"],
    )
    github_api.responds("POST", f"{ISSUES}/{ISSUE_NUMBER}/labels", json=[{"name": NEEDS_HUMAN}])
    github_api.responds("DELETE", f"{ISSUES}/{ISSUE_NUMBER}/labels/devin%3Aautofix", json=[])
    github_api.responds(
        "GET", f"{PULLS}/{PR_NUMBER}", json=github_payload("pull_request.opened")["pull_request"]
    )
    github_api.responds(
        "POST",
        f"{PULLS}/{PR_NUMBER}/requested_reviewers",
        201,
        json={"requested_reviewers": [{"login": "taxpon"}]},
    )
    github_api.responds(
        "GET",
        f"/repos/{REPO}/commits/{HEAD_SHA}/check-runs",
        json={"total_count": 1, "check_runs": [a_check_run()]},
    )
    fake_ci(github_api, jobs=[a_job()])
