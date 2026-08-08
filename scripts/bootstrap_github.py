"""Prepare the target repository for the pipeline: issues, labels and the delivery webhook.

`docs/09-operations.md#bootstrap` is the specification — the `gh` invocations there, made
re-runnable. This is not a one-shot script in practice: a free `cloudflared` tunnel hands out a
different URL every time it restarts ([B9](../docs/blockers.md)), so pointing the webhook somewhere
new is the ordinary case rather than the exceptional one.

    uv run scripts/bootstrap_github.py --dry-run
    uv run scripts/bootstrap_github.py
    uv run scripts/bootstrap_github.py --webhook-url https://kind-otter.trycloudflare.com

Every step reconciles rather than recreates. Issues are enabled only when they are off, a label is
patched only in the fields that differ, and the webhook Sentinel already owns — recognised by the
receiver path it points at, not by the host, which is what changes — is updated in place. Run
without `--webhook-url`, as `make bootstrap` does, the hook is still reconciled: everything but its
URL is brought into line, including the secret.

Where several hooks deliver to the receiver path, the one already at the target URL is the one
kept, never simply the oldest. See
`docs/adr/2026-08-09-the-webhook-already-at-the-target-url-is-the-one-kept.md`: repointing a dead
hook at the tunnel a working hook already uses does not consolidate them, it doubles delivery.

**Nothing here deletes.** A second run has to be survivable against a repository somebody else has
also configured, and delete-then-recreate is the one shape that is not: it turns a re-run into data
loss, and a webhook recreated behind a dead tunnel is indistinguishable from one that was never
registered. Anything unexpected is reported and left alone.

Nothing here retries, either. A failed step leaves the repository in a state the next run completes,
which is the property worth having for a command an operator is watching; the retry policy belongs
to `sentinel.github.client`, where a failure is unattended.

The webhook secret is written to GitHub and never read back — `config.secret` comes back masked —
so it is never compared, never reported and never rendered. See
`docs/adr/2026-08-08-the-bootstrap-writes-the-webhook-secret-it-cannot-read.md`.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urlparse

import httpx
from pydantic import SecretStr

from sentinel.config import ConfigurationError, Settings, get_settings
from sentinel.devin.playbooks import IssueClass
from sentinel.github.client import API_VERSION, GITHUB_API_BASE
from sentinel.observability.logging import REDACTED, secret_values

WEBHOOK_PATH: Final = "/webhooks/github"
"""Where deliveries land (`docs/06-event-pipeline.md`). Also how the hook Sentinel owns is told
apart from any other hook on the repository: the host is a tunnel and rotates, the path does not."""

WEBHOOK_EVENTS: Final[tuple[str, ...]] = (
    "check_suite",
    "issue_comment",
    "issues",
    "pull_request",
    "pull_request_review",
)
"""The subscription in `docs/06-event-pipeline.md#subscribed-events`. `ping` is in that table but
not here: GitHub sends it on registration whether or not it was asked for, and naming it in the
subscription is rejected. A hook registered with the wrong list fails silently — the deliveries
that never arrive leave no trace on the receiving end — so `tests/test_bootstrap_github.py` reads
the list back out of the spec table rather than restating it."""

TRIGGER_LABEL_COLOR: Final = "0e8a16"
ESCALATION_LABEL: Final = "needs-human"
ESCALATION_LABEL_COLOR: Final = "d93f0b"
CLASS_LABEL_PREFIX: Final = "class:"
CLASS_LABEL_COLOR: Final = "5319e7"

REQUEST_TIMEOUT_SECONDS: Final = 30.0
PAGE_SIZE: Final = 100
BODY_EXCERPT_CHARS: Final = 400

EXIT_STEP_FAILED: Final = 1
EXIT_MISCONFIGURED: Final = 2


class BootstrapError(RuntimeError):
    """A step did not complete. Whatever ran before it stands; re-running finishes the rest."""


@dataclass(frozen=True, slots=True)
class Label:
    """A label the repository must carry, and the fields this script is prepared to assert.

    `description` of `None` means *unmanaged*: the label's colour is reconciled and its description
    is left however a human wrote it. The eight `class:` labels are declared that way, because
    `docs/09-operations.md` creates them with no description at all, and overwriting someone's text
    with an empty string is a deletion wearing an update's clothes.
    """

    name: str
    color: str
    description: str | None = None

    def creation(self) -> dict[str, str]:
        body = {"name": self.name, "color": self.color}
        if self.description is not None:
            body["description"] = self.description
        return body

    def differences(self, current: Mapping[str, Any]) -> dict[str, str]:
        """The fields of an existing label that do not match this one — the whole PATCH body.

        Only what differs is sent, so a run that has nothing to change makes no request at all and
        an unmanaged description survives a colour correction.
        """
        changes: dict[str, str] = {}
        if _hex(current.get("color")) != _hex(self.color):
            changes["color"] = self.color
        if self.description is not None and _text(current.get("description")) != self.description:
            changes["description"] = self.description
        return changes


def _hex(value: object) -> str:
    """A colour as GitHub stores it. `#0E8A16` from a human and `0e8a16` from the API are equal."""
    return _text(value).removeprefix("#").lower()


def _text(value: object) -> str:
    """GitHub returns `null` for a description that was never set; so does an absent key."""
    return "" if value is None else str(value)


def _hint(response: httpx.Response) -> str:
    """What to do about a `403`, which GitHub uses for two unrelated things.

    A fine-grained PAT missing a permission is answered with "Resource not accessible by personal
    access token" and no indication of *which* permission — and the token `docs/09-operations.md`
    specifies is scoped to issues, pull requests and contents, none of which covers enabling the
    issue tracker or managing a webhook. Being rate limited looks the same from the status line, so
    the two are told apart by the headers rather than left ambiguous.
    """
    if response.status_code == 429 or (
        response.status_code == 403
        and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
        )
    ):
        return " — rate limited; wait and re-run."
    if response.status_code == 403:
        return (
            " — check the token's permissions: enabling issues needs Administration: write, "
            "and the webhook needs Webhooks: read and write."
        )
    return ""


def desired_labels(settings: Settings) -> tuple[Label, ...]:
    """The label set from `docs/09-operations.md#bootstrap`.

    The trigger label is read from the configuration rather than written out, so that bootstrapping
    the repository and dispatching on the label cannot disagree about its name — a mismatch there is
    a pipeline that never starts and reports nothing (`docs/09-operations.md#troubleshooting`). The
    eight class labels are generated from `IssueClass` for the same reason: a class the worker can
    parse but the repository cannot express would only surface on the issue that used it.
    """
    return (
        Label(settings.autofix_label, TRIGGER_LABEL_COLOR, "Sentinel: remediate automatically"),
        Label(ESCALATION_LABEL, ESCALATION_LABEL_COLOR, "Sentinel: escalated"),
        *(
            Label(f"{CLASS_LABEL_PREFIX}{issue_class.value}", CLASS_LABEL_COLOR)
            for issue_class in IssueClass
        ),
    )


class GitHub:
    """The subset of the REST API this script needs, over one connection.

    Deliberately not `sentinel.github.client.GitHubClient`: that client's `ROUTES` table is the
    statement of everything the running pipeline can do to the repository, and repository settings,
    labels and webhooks are administration rather than remediation. See
    `docs/adr/2026-08-08-the-bootstrap-script-does-not-borrow-the-pipelines-github-client.md`.
    """

    def __init__(
        self,
        http: httpx.Client,
        repo: str,
        *,
        secrets: frozenset[str],
        dry_run: bool = False,
    ) -> None:
        self._http = http
        self._secrets = secrets
        self.repo = repo
        self.dry_run = dry_run

    def __repr__(self) -> str:
        # Names the repository the run is about, where the default names an address.
        return f"GitHub(repo={self.repo!r})"

    @property
    def repo_path(self) -> str:
        return f"/repos/{self.repo}"

    def get(self, path: str) -> dict[str, Any]:
        response = self._request("GET", path)
        body = self._decoded(response, "GET", path)
        if not isinstance(body, dict):
            raise BootstrapError(f"GET {path} did not answer with an object")
        return body

    def collection(self, path: str) -> list[dict[str, Any]]:
        """Every item of a paginated collection, following `Link: rel="next"`.

        Superset's fork inherits well over a hundred labels, so stopping at the first page would
        make an existing label look missing — and this script would then try to create it, which is
        precisely the duplicate the exercise is about.
        """
        items: list[dict[str, Any]] = []
        url: str | None = path
        params: dict[str, Any] | None = {"per_page": PAGE_SIZE}
        while url is not None:
            response = self._request("GET", url, params=params)
            page = self._decoded(response, "GET", url)
            if not isinstance(page, list):
                raise BootstrapError(f"GET {url} did not answer with an array")
            items += page
            following = response.links.get("next")
            # The next URL carries the cursor and the page size already.
            url, params = (following["url"], None) if following else (None, None)
        return items

    def write(self, method: str, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """One mutating call, or nothing at all under `--dry-run`.

        The body is never rendered anywhere: for the webhook it holds the shared secret, and a
        dry run exists to be pasted into a terminal transcript.
        """
        if self.dry_run:
            return {}
        response = self._request(method, path, json=body)
        decoded = self._decoded(response, method, path) if response.content else {}
        return decoded if isinstance(decoded, dict) else {}

    def change(self, message: str) -> None:
        """Report one line of what happened, marked when it did not actually happen."""
        print(f"  {'[dry run] ' if self.dry_run else ''}{message}")

    def note(self, message: str) -> None:
        """Report something the operator has to decide about. Never fatal, never acted on."""
        print(f"  note: {message}", file=sys.stderr)

    def _request(self, method: str, path: str, **options: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **options)
        except httpx.HTTPError as exc:
            # `from None` rather than `from exc`: the chained exception carries the request, and
            # the request carries the Authorization header.
            raise BootstrapError(self._redact(f"{method} {path} failed: {exc}")) from None
        if response.is_success:
            return response
        raise BootstrapError(
            f"{method} {path} failed with {response.status_code}: "
            f"{self._excerpt(response)}{_hint(response)}"
        )

    def _decoded(self, response: httpx.Response, method: str, path: str) -> Any:
        """The JSON body, or a `BootstrapError` naming the request that produced something else.

        A tunnel or a proxy in front of GitHub answers `200 text/html`. Left to `response.json()`
        that is a `JSONDecodeError` raised past `main`'s handler, so the operator gets a traceback
        instead of the one line saying the run is resumable.
        """
        try:
            return response.json()
        except ValueError:
            raise BootstrapError(
                f"{method} {path} did not answer with JSON: {self._excerpt(response)}"
            ) from None

    def _excerpt(self, response: httpx.Response) -> str:
        """A redacted, single-line excerpt of a response body, for an error message."""
        text = self._redact(" ".join(response.text.split()))
        return text[:BODY_EXCERPT_CHARS] if text else "no response body"

    def _redact(self, text: str) -> str:
        """Take every configured credential back out of anything on its way to a terminal.

        GitHub echoes a rejected payload back in the `errors` of a `422`, and the payload that gets
        rejected most often here is the webhook's — the one carrying the secret. The structured
        logger's redaction does not cover a script that prints.
        """
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


def enable_issues(api: GitHub) -> None:
    """B1: a fork has its issue tracker off, and `issues.labeled` is the pipeline's only trigger."""
    if api.get(api.repo_path).get("has_issues"):
        print("issues: already enabled")
        return
    api.write("PATCH", api.repo_path, {"has_issues": True})
    api.change("issues: enabled")


def sync_labels(api: GitHub, labels: Sequence[Label]) -> None:
    """Create what is missing, correct what differs, and touch nothing else."""
    print(f"labels: reconciling {len(labels)}")
    # GitHub treats label names case-insensitively — `Class:bug` and `class:bug` cannot coexist —
    # so a case-sensitive lookup would report a label as missing and then fail to create it.
    existing = {
        _text(label.get("name")).lower(): label
        for label in api.collection(f"{api.repo_path}/labels")
    }
    for label in labels:
        current = existing.get(label.name.lower())
        if current is None:
            api.write("POST", f"{api.repo_path}/labels", label.creation())
            api.change(f"label {label.name}: created")
            continue
        changes = label.differences(current)
        if not changes:
            continue
        # Addressed by the name it is stored under, not by the name we wanted: a PATCH to
        # `class:Bug` when the repository holds `class:bug` is a 404, and renaming needs `new_name`.
        path = f"{api.repo_path}/labels/{quote(_text(current.get('name')), safe='')}"
        api.write("PATCH", path, changes)
        api.change(f"label {label.name}: updated {', '.join(sorted(changes))}")


def sync_webhook(api: GitHub, *, url: str | None, secret: SecretStr) -> None:
    """Register the delivery webhook, move the existing one to a new tunnel, or repair it."""
    hooks = [hook for hook in api.collection(f"{api.repo_path}/hooks") if _is_sentinel_hook(hook)]

    if not hooks:
        if url is None:
            print("webhook: not registered")
            api.note(
                "no webhook delivers to this pipeline. Start a tunnel and re-run with "
                "--webhook-url <https://…>."
            )
            return
        api.write("POST", f"{api.repo_path}/hooks", {"name": "web", **_body(url, secret)})
        api.change(f"webhook: registered {url}")
        return

    hook = _canonical(hooks, url)
    hook_id = hook.get("id")
    # With no URL to move it to, the hook is reconciled against the one it already has. That is
    # the path `make bootstrap` takes, and everything but the URL still has to be brought into
    # line — above all the secret, which is the field a run cannot see and cannot skip.
    target = url if url is not None else _hook_url(hook)
    if url is None:
        print(f"webhook: reconciling {hook_id} where it is ({target}); no --webhook-url given")

    differences = _hook_differences(hook, target)
    # Written every run, even when nothing observably differs. `config.secret` comes back from
    # GitHub as `********`, so "the hook already has the right secret" is not a knowable state, and
    # a PATCH replaces `config` wholesale — omitting the secret would delete it from a working
    # hook. Re-sending the same values changes nothing and creates nothing.
    api.write("PATCH", f"{api.repo_path}/hooks/{hook_id}", _body(target, secret))
    api.change(
        f"webhook {hook_id}: updated {', '.join(differences)}"
        if differences
        else f"webhook {hook_id}: already current at {target}; secret re-applied"
    )
    for other in hooks:
        if other is hook:
            continue
        doubled = " — every event is delivered twice" if _hook_url(other) == target else ""
        api.note(
            f"webhook {other.get('id')} also delivers to this pipeline, at "
            f"{_hook_url(other)}{doubled}. Only {hook_id} is reconciled; delete the other "
            "yourself once you are sure of it."
        )


def _body(url: str, secret: SecretStr) -> dict[str, Any]:
    """The whole of a hook's configuration. `PATCH` replaces `config`, so it is sent whole."""
    return {
        "active": True,
        "events": list(WEBHOOK_EVENTS),
        "config": {
            "url": url,
            "content_type": "json",
            # A delivery signed with the shared secret still travels over the wire, and GitHub
            # will happily post to a certificate it cannot verify if asked to.
            "insecure_ssl": "0",
            "secret": secret.get_secret_value(),
        },
    }


def _canonical(hooks: Sequence[Mapping[str, Any]], url: str | None) -> Mapping[str, Any]:
    """Which of several hooks on the receiver path is the one to keep.

    The one already delivering to `url`, if there is one; otherwise the oldest, which is the order
    GitHub lists them in. Picking the oldest unconditionally is what turns "one dead hook and one
    live hook" into *two* live hooks: repointing the dead one at the tunnel the live one already
    uses does not consolidate them, it doubles delivery — and a doubled `check_suite.completed`
    resumes the same Devin session twice, spending the ACUs and the fix cycle twice
    (`docs/06-event-pipeline.md`, `docs/04-state-machine.md`). Only the domain layer's
    `(repo, issue_number)` uniqueness absorbs a doubled *start*; nothing absorbs a doubled resume.

    With no URL given there is nothing to prefer by — no hook is being moved — so the oldest it is.
    """
    return next((hook for hook in hooks if _hook_url(hook) == url), hooks[0])


def _is_sentinel_hook(hook: Mapping[str, Any]) -> bool:
    return _url_path(_hook_url(hook)) == WEBHOOK_PATH


def _hook_url(hook: Mapping[str, Any]) -> str:
    config = hook.get("config")
    return _text(config.get("url")) if isinstance(config, Mapping) else ""


def _hook_differences(hook: Mapping[str, Any], url: str) -> list[str]:
    """What is about to change about an existing hook, in terms an operator can act on.

    The secret is not in here and cannot be: GitHub masks it on the way out, so there is nothing to
    compare against and nothing that could be reported without printing it.
    """
    config = hook.get("config")
    config = config if isinstance(config, Mapping) else {}
    differences: list[str] = []
    if _text(config.get("url")) != url:
        differences.append(f"url -> {url}")
    if _text(config.get("content_type")) != "json":
        differences.append("content_type -> json")
    if _text(config.get("insecure_ssl")) not in {"0", "0.0"}:
        differences.append("insecure_ssl -> 0")
    if hook.get("active") is not True:
        differences.append("active -> true")
    if {_text(event) for event in hook.get("events") or []} != set(WEBHOOK_EVENTS):
        differences.append(f"events -> {', '.join(WEBHOOK_EVENTS)}")
    return differences


def _url_path(url: str) -> str:
    return urlparse(url).path.rstrip("/")


def webhook_url(given: str) -> str:
    """The delivery URL, from either the tunnel's root or the full receiver URL.

    `cloudflared` prints the root, `docs/09-operations.md` appends the path in the shell, and an
    operator re-running this after a tunnel restart has whichever of the two is in their history.
    Both are accepted and both produce the same URL, so pasting the wrong one cannot register a
    second hook at `…/webhooks/github/webhooks/github`.
    """
    trimmed = given.strip().rstrip("/")
    parsed = urlparse(trimmed)
    if parsed.scheme != "https" or not parsed.netloc:
        raise BootstrapError(
            f"--webhook-url must be a public https URL, got {given!r}. "
            "GitHub will not deliver to http, and the signature travels with the payload."
        )
    return trimmed if _url_path(trimmed) == WEBHOOK_PATH else f"{trimmed}{WEBHOOK_PATH}"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare the target repository: issues, labels and the delivery webhook."
    )
    parser.add_argument(
        "--webhook-url",
        # `docs/09-operations.md` already puts the tunnel URL in `$TUNNEL_URL`; reading it means the
        # documented copy-paste flow works with no flag, and no new variable to document.
        default=os.environ.get("TUNNEL_URL"),
        metavar="URL",
        help="Public https URL of the tunnel, with or without the /webhooks/github path. "
        "Defaults to $TUNNEL_URL. Omitted, an existing webhook is left exactly as it is.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return EXIT_MISCONFIGURED

    print(f"repository {settings.target_repo}{' (dry run)' if args.dry_run else ''}")
    try:
        url = webhook_url(args.webhook_url) if args.webhook_url else None
        with httpx.Client(
            base_url=GITHUB_API_BASE,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                # Distinct from the pipeline's `sentinel`, so the repository's audit log separates
                # what an operator did at bootstrap from what the running system does.
                "User-Agent": "sentinel-bootstrap",
            },
        ) as http:
            api = GitHub(
                http,
                settings.target_repo,
                secrets=secret_values(settings),
                dry_run=args.dry_run,
            )
            enable_issues(api)
            sync_labels(api, desired_labels(settings))
            sync_webhook(api, url=url, secret=settings.github_webhook_secret)
    except BootstrapError as exc:
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        print("Nothing is undone; re-run to complete the remaining steps.", file=sys.stderr)
        return EXIT_STEP_FAILED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
