"""`scripts/bootstrap_github.py` is the only code here that writes to `taxpon/superset` — a public
fork of a real project — so these tests are the whole of the evidence that it is safe to run. It is
never run against the real repository to find out.

Two properties carry the weight, and neither is visible in a response body:

- **Idempotence.** The tunnel URL rotates ([B9](../docs/blockers.md)), so this is re-run routinely.
  A `FakeRepo` below holds the repository's state and *applies* the writes it receives, which is
  what lets a test run the script twice and assert on what the second run did — a stateless mock
  would answer the second run exactly as it answered the first and prove nothing.
- **The request bodies.** A webhook registered with four of the five events, or without
  `content_type: json`, answers `201` and looks perfect from the outside; what it costs is the
  deliveries that never arrive, which leave no trace anywhere. Every assertion here is on the
  captured request, per `.claude/rules/testing.md`, and the event list is read back out of the spec
  table rather than restated.

The secret is asserted twice over: that it *is* sent to GitHub, and that it reaches no other
destination — not stdout, not stderr, not an error message quoting a payload GitHub rejected.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import respx

from conftest import GITHUB_API_BASE, GITHUB_TOKEN, WEBHOOK_SECRET, FakeAPI, make_settings
from factories import REPO
from sentinel.config import ConfigurationError, Settings
from sentinel.devin.playbooks import IssueClass

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    """Import a file from `scripts/`, which is not a package and is not on the path.

    Imported rather than run as a subprocess — unlike `tests/test_gen_adr_index.py`, which has a
    filesystem to assert on. Here the whole subject is the HTTP traffic, and `respx` patches
    transports in this process.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed: `@dataclass` resolves its annotations through
    # `sys.modules[cls.__module__]`, and a module not yet there fails at class creation.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = _load("bootstrap_github")

TUNNEL = "https://kind-otter.trycloudflare.com"
DELIVERY_URL = f"{TUNNEL}/webhooks/github"

CLASS_LABELS = tuple(f"class:{issue_class.value}" for issue_class in IssueClass)
EXPECTED_LABELS = ("devin:autofix", "needs-human", *CLASS_LABELS)


# ------------------------------------------------------------------------------ the fake fork


@dataclass
class FakeRepo:
    """`taxpon/superset` as far as this script can tell: state, and the writes applied to it.

    Only the fields the script reads are modelled, and the webhook secret is masked on the way out
    exactly as GitHub masks it — which is what makes "the hook already has the right secret"
    unknowable here too, rather than an artefact the tests could accidentally rely on.
    """

    has_issues: bool = False
    labels: list[dict[str, Any]] = field(default_factory=list)
    hooks: list[dict[str, Any]] = field(default_factory=list)
    page_size: int | None = None
    broken: tuple[str, str] | None = None
    """`(method, path suffix)` to answer `500`, for the partial-failure tests."""
    broken_after: int = 0
    """How many matching calls succeed before `broken` starts failing them."""
    error_status: int = 500
    error_body: dict[str, Any] = field(default_factory=lambda: {"message": "Server Error"})
    error_text: str | None = None
    """A non-JSON body, for the proxy-in-front-of-GitHub case. Wins over `error_body`."""
    error_headers: dict[str, str] = field(default_factory=dict)

    _matched: int = 0
    _next_hook_id: int = 1

    # --- wiring ---

    def install(self, router: respx.MockRouter, repo: str = REPO) -> FakeRepo:
        base = f"{GITHUB_API_BASE}/repos/{repo}"
        router.get(base).mock(side_effect=self._guard(self._repository))
        router.patch(base).mock(side_effect=self._guard(self._update_repository))
        router.get(f"{base}/labels").mock(side_effect=self._guard(self._list_labels))
        router.post(f"{base}/labels").mock(side_effect=self._guard(self._create_label))
        router.patch(url__regex=rf"{re.escape(base)}/labels/.+").mock(
            side_effect=self._guard(self._update_label)
        )
        router.get(f"{base}/hooks").mock(side_effect=self._guard(self._list_hooks))
        router.post(f"{base}/hooks").mock(side_effect=self._guard(self._create_hook))
        router.patch(url__regex=rf"{re.escape(base)}/hooks/\d+").mock(
            side_effect=self._guard(self._update_hook)
        )
        return self

    def _guard(self, handler: Any) -> Any:
        def respond(request: httpx.Request) -> httpx.Response:
            if (
                self.broken
                and request.method == self.broken[0]
                and request.url.path.endswith(self.broken[1])
            ):
                self._matched += 1
                if self._matched > self.broken_after:
                    if self.error_text is not None:
                        return httpx.Response(
                            self.error_status, text=self.error_text, headers=self.error_headers
                        )
                    return httpx.Response(
                        self.error_status, json=self.error_body, headers=self.error_headers
                    )
            return handler(request)

        return respond

    # --- the repository ---

    def _repository(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"full_name": REPO, "has_issues": self.has_issues})

    def _update_repository(self, request: httpx.Request) -> httpx.Response:
        self.has_issues = bool(_body(request).get("has_issues", self.has_issues))
        return self._repository(request)

    # --- labels ---

    def _list_labels(self, request: httpx.Request) -> httpx.Response:
        return self._page(request, self.labels)

    def _create_label(self, request: httpx.Request) -> httpx.Response:
        body = _body(request)
        if self._label(body["name"]) is not None:
            return httpx.Response(
                422, json={"message": "Validation Failed", "errors": [{"code": "already_exists"}]}
            )
        label = {
            "name": body["name"],
            "color": body["color"],
            "description": body.get("description"),
        }
        self.labels.append(label)
        return httpx.Response(201, json=label)

    def _update_label(self, request: httpx.Request) -> httpx.Response:
        label = self._label(unquote(request.url.path.rsplit("/", 1)[-1]))
        if label is None:
            return httpx.Response(404, json={"message": "Not Found"})
        label.update(_body(request))
        return httpx.Response(200, json=label)

    def label(self, name: str) -> dict[str, Any]:
        """The stored label of that name, matched as GitHub matches it: without regard to case."""
        found = self._label(name)
        assert found is not None, f"{name} is not on the repository: {self.label_names()}"
        return found

    def _label(self, name: str) -> dict[str, Any] | None:
        return next((it for it in self.labels if it["name"].lower() == name.lower()), None)

    def label_names(self) -> list[str]:
        return [label["name"] for label in self.labels]

    # --- webhooks ---

    def _list_hooks(self, request: httpx.Request) -> httpx.Response:
        return self._page(request, self.hooks)

    def _create_hook(self, request: httpx.Request) -> httpx.Response:
        body = _body(request)
        hook = {
            "id": self._next_hook_id,
            "name": body.get("name", "web"),
            "active": body["active"],
            "events": body["events"],
            "config": _masked(body["config"]),
        }
        self._next_hook_id += 1
        self.hooks.append(hook)
        return httpx.Response(201, json=hook)

    def _update_hook(self, request: httpx.Request) -> httpx.Response:
        hook_id = int(request.url.path.rsplit("/", 1)[-1])
        hook = next((it for it in self.hooks if it["id"] == hook_id), None)
        if hook is None:
            return httpx.Response(404, json={"message": "Not Found"})
        body = _body(request)
        hook["active"] = body.get("active", hook["active"])
        hook["events"] = body.get("events", hook["events"])
        hook["config"] = _masked({**hook["config"], **body.get("config", {})})
        return httpx.Response(200, json=hook)

    # --- pagination ---

    def _page(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        if self.page_size is None:
            return httpx.Response(200, json=items)
        page = int(request.url.params.get("page", 1))
        start = (page - 1) * self.page_size
        headers = {}
        if start + self.page_size < len(items):
            following = request.url.copy_set_param("page", page + 1)
            headers["Link"] = f'<{following}>; rel="next"'
        return httpx.Response(200, json=items[start : start + self.page_size], headers=headers)


def _masked(config: dict[str, Any]) -> dict[str, Any]:
    """What GitHub gives back: everything about the hook except the secret it was given."""
    return {**config, "secret": "********"} if "secret" in config else dict(config)


def _body(request: httpx.Request) -> dict[str, Any]:
    body = json.loads(request.content)
    assert isinstance(body, dict)
    return body


def a_hook(
    *,
    id: int = 1,
    url: str = DELIVERY_URL,
    events: tuple[str, ...] | None = None,
    content_type: str = "json",
    insecure_ssl: str = "0",
    active: bool = True,
) -> dict[str, Any]:
    return {
        "id": id,
        "name": "web",
        "active": active,
        "events": list(bootstrap.WEBHOOK_EVENTS if events is None else events),
        "config": {
            "url": url,
            "content_type": content_type,
            "insecure_ssl": insecure_ssl,
            "secret": "********",
        },
    }


# ------------------------------------------------------------------------------ running it


Run = Any


@pytest.fixture
def repo(http_mock: respx.MockRouter) -> FakeRepo:
    """A fresh fork: issues off, no labels, no webhooks."""
    return FakeRepo().install(http_mock)


@pytest.fixture
def run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Run:
    """Invoke the script the way the Makefile does, minus the process boundary.

    `$TUNNEL_URL` is cleared so that a developer who has one exported cannot change what the tests
    mean, and restored by monkeypatch afterwards.
    """
    monkeypatch.delenv("TUNNEL_URL", raising=False)
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)

    def invoke(*argv: str, config: Settings | None = None) -> int:
        if config is not None:
            monkeypatch.setattr(bootstrap, "get_settings", lambda: config)
        return int(bootstrap.main(list(argv)))

    return invoke


# ------------------------------------------------------------------------------ issues (B1)


def test_enables_issues_on_a_fresh_fork(repo: FakeRepo, run: Run, github_api: FakeAPI) -> None:
    assert run() == 0

    assert github_api.only("PATCH", f"/repos/{REPO}").json == {"has_issues": True}
    assert repo.has_issues is True


def test_leaves_issues_alone_when_they_are_already_on(
    repo: FakeRepo, run: Run, github_api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.has_issues = True

    assert run() == 0

    assert github_api.sent("PATCH", f"/repos/{REPO}") == []
    assert "issues: already enabled" in capsys.readouterr().out


# ------------------------------------------------------------------------------ labels


def test_creates_the_label_set_the_spec_names(
    repo: FakeRepo, run: Run, github_api: FakeAPI
) -> None:
    assert run() == 0

    created = [request.json for request in github_api.sent("POST", f"/repos/{REPO}/labels")]
    assert created == [
        {
            "name": "devin:autofix",
            "color": "0e8a16",
            "description": "Sentinel: remediate automatically",
        },
        {"name": "needs-human", "color": "d93f0b", "description": "Sentinel: escalated"},
        *({"name": name, "color": "5319e7"} for name in CLASS_LABELS),
    ]
    assert [label["name"] for label in repo.labels] == list(EXPECTED_LABELS)


def test_class_labels_cover_every_issue_class(repo: FakeRepo, run: Run) -> None:
    """A class the worker can parse but the fork cannot express only surfaces on the issue that
    used it, by which point the remediation has already been dispatched."""
    assert run() == 0

    labelled = {
        name.removeprefix("class:") for name in repo.label_names() if name.startswith("class:")
    }
    assert labelled == {issue_class.value for issue_class in IssueClass}


def test_the_trigger_label_follows_the_configured_name(
    repo: FakeRepo, run: Run, github_api: FakeAPI
) -> None:
    """`AUTOFIX_LABEL` is what the pipeline dispatches on; bootstrapping a different name is a
    pipeline that never starts and reports nothing."""
    assert run(config=make_settings(autofix_label="ops:fixme")) == 0

    names = [request.json["name"] for request in github_api.sent("POST", f"/repos/{REPO}/labels")]
    assert "ops:fixme" in names
    assert "devin:autofix" not in names


def test_corrects_a_label_whose_colour_drifted(
    repo: FakeRepo, run: Run, github_api: FakeAPI
) -> None:
    repo.labels.append({"name": "needs-human", "color": "ededed", "description": None})

    assert run() == 0

    request = github_api.only("PATCH", f"/repos/{REPO}/labels/needs-human")
    assert request.json == {"color": "d93f0b", "description": "Sentinel: escalated"}
    assert github_api.sent("POST", f"/repos/{REPO}/labels") != []
    assert repo.label("needs-human") == {
        "name": "needs-human",
        "color": "d93f0b",
        "description": "Sentinel: escalated",
    }


def test_corrects_only_the_field_that_drifted(
    repo: FakeRepo, run: Run, github_api: FakeAPI
) -> None:
    repo.labels.append(
        {"name": "devin:autofix", "color": "0e8a16", "description": "something else"}
    )

    assert run() == 0

    request = github_api.only("PATCH", f"/repos/{REPO}/labels/devin:autofix")
    assert request.json == {"description": "Sentinel: remediate automatically"}


def test_leaves_a_label_that_already_matches(repo: FakeRepo, run: Run, github_api: FakeAPI) -> None:
    """`#0E8A16` typed by a human and `0e8a16` stored by GitHub are the same colour. Treating them
    as different would make every run write, which is how a "no-op" bootstrap starts editing."""
    repo.labels.append(
        {
            "name": "devin:autofix",
            "color": "#0E8A16",
            "description": "Sentinel: remediate automatically",
        }
    )

    assert run() == 0

    assert github_api.sent("PATCH", f"/repos/{REPO}/labels/devin:autofix") == []


def test_does_not_erase_a_description_a_human_wrote(
    repo: FakeRepo, run: Run, github_api: FakeAPI
) -> None:
    """The spec creates the `class:` labels with no description, so this script does not manage
    theirs. Writing an empty string over somebody's text is a deletion wearing an update's
    clothes — the colour is still corrected."""
    repo.labels.append({"name": "class:perf", "color": "ededed", "description": "N+1 queries"})

    assert run() == 0

    patched = github_api.only("PATCH", f"/repos/{REPO}/labels/class:perf")
    assert patched.json == {"color": "5319e7"}
    assert repo.label("class:perf")["description"] == "N+1 queries"


def test_matches_an_existing_label_whatever_its_case(
    repo: FakeRepo, run: Run, github_api: FakeAPI
) -> None:
    """GitHub will not hold `Needs-Human` and `needs-human` at once, so a case-sensitive lookup
    would report the label missing and then fail to create it."""
    repo.labels.append(
        {"name": "Needs-Human", "color": "d93f0b", "description": "Sentinel: escalated"}
    )

    assert run() == 0

    names = [request.json["name"] for request in github_api.sent("POST", f"/repos/{REPO}/labels")]
    assert "needs-human" not in names
    assert len(repo.labels) == len(EXPECTED_LABELS)


def test_addresses_a_drifted_label_by_its_stored_name(
    repo: FakeRepo, run: Run, github_api: FakeAPI
) -> None:
    """A PATCH to `needs-human` when the repository holds `Needs-Human` is a 404."""
    repo.labels.append({"name": "Needs-Human", "color": "ededed", "description": None})

    assert run() == 0

    assert github_api.only("PATCH", f"/repos/{REPO}/labels/Needs-Human").json["color"] == "d93f0b"


def test_finds_a_label_that_is_not_on_the_first_page(
    repo: FakeRepo, run: Run, github_api: FakeAPI
) -> None:
    """The fork inherits Superset's labels — well over one page of them. A label the script cannot
    see is a label it tries to create, which is the duplicate this is all about."""
    repo.page_size = 3
    repo.labels = [
        {"name": f"inherited-{n}", "color": "ededed", "description": None} for n in range(7)
    ]
    repo.labels.append({"name": "needs-human", "color": "ededed", "description": None})

    assert run() == 0

    assert github_api.sent("POST", f"/repos/{REPO}/labels/needs-human") == []
    assert github_api.only("PATCH", f"/repos/{REPO}/labels/needs-human").json["color"] == "d93f0b"
    assert [
        request.json["name"] for request in github_api.sent("POST", f"/repos/{REPO}/labels")
    ] == [
        "devin:autofix",
        *CLASS_LABELS,
    ]


# ------------------------------------------------------------------------------ the webhook


def spec_events() -> set[str]:
    """The subscription table in `docs/06-event-pipeline.md`, read rather than restated.

    A hook registered with the wrong event list answers `201` and looks correct; what it costs is
    the deliveries that never arrive. Deriving the expectation from the spec means adding an event
    to the pipeline without subscribing to it fails here.
    """
    text = (ROOT / "docs" / "06-event-pipeline.md").read_text()
    section = text.split("## Subscribed events", 1)[1].split("\n## ", 1)[0]
    return {
        match.group(1)
        for line in section.splitlines()
        if (match := re.match(r"\|\s*`([a-z_]+)`", line))
    }


def test_the_event_list_is_the_one_the_pipeline_handles() -> None:
    # `ping` is in the table but cannot be subscribed to: GitHub sends it on registration whether
    # or not it was asked for.
    assert spec_events() - {"ping"} == set(bootstrap.WEBHOOK_EVENTS)


def test_registers_the_webhook(repo: FakeRepo, run: Run, github_api: FakeAPI) -> None:
    assert run("--webhook-url", TUNNEL) == 0

    assert github_api.only("POST", f"/repos/{REPO}/hooks").json == {
        "name": "web",
        "active": True,
        "events": list(bootstrap.WEBHOOK_EVENTS),
        "config": {
            "url": DELIVERY_URL,
            "content_type": "json",
            "insecure_ssl": "0",
            "secret": WEBHOOK_SECRET,
        },
    }
    assert len(repo.hooks) == 1


@pytest.mark.parametrize(
    "given",
    [
        TUNNEL,
        f"{TUNNEL}/",
        DELIVERY_URL,
        f"{DELIVERY_URL}/",
        f"  {TUNNEL}  ",
    ],
)
def test_accepts_the_tunnel_root_or_the_full_delivery_url(
    repo: FakeRepo, run: Run, github_api: FakeAPI, given: str
) -> None:
    """Whichever of the two an operator has in their shell history has to mean the same thing;
    otherwise the wrong one registers a hook at `…/webhooks/github/webhooks/github`."""
    assert run("--webhook-url", given) == 0

    assert github_api.only("POST", f"/repos/{REPO}/hooks").json["config"]["url"] == DELIVERY_URL


@pytest.mark.parametrize(
    "given", ["http://kind-otter.trycloudflare.com", "kind-otter.trycloudflare.com", "https://"]
)
def test_rejects_a_url_that_is_not_public_https(
    repo: FakeRepo, run: Run, github_api: FakeAPI, capsys: pytest.CaptureFixture[str], given: str
) -> None:
    assert run("--webhook-url", given) == 1

    assert github_api.requests == []
    assert "must be a public https URL" in capsys.readouterr().err


def test_takes_the_tunnel_url_from_the_environment(
    repo: FakeRepo, run: Run, github_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`docs/09-operations.md` already puts it in `$TUNNEL_URL`; the documented copy-paste works."""
    monkeypatch.setenv("TUNNEL_URL", TUNNEL)

    assert run() == 0

    assert github_api.only("POST", f"/repos/{REPO}/hooks").json["config"]["url"] == DELIVERY_URL


def test_moves_an_existing_hook_to_the_new_tunnel(
    repo: FakeRepo, run: Run, github_api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole reason this script is re-run. A second hook behind a dead tunnel would look
    identical from the dashboard and deliver nothing."""
    repo.hooks.append(a_hook(id=7, url="https://stale-tunnel.trycloudflare.com/webhooks/github"))

    assert run("--webhook-url", TUNNEL) == 0

    assert github_api.sent("POST", f"/repos/{REPO}/hooks") == []
    assert github_api.only("PATCH", f"/repos/{REPO}/hooks/7").json["config"]["url"] == DELIVERY_URL
    assert len(repo.hooks) == 1
    assert repo.hooks[0]["config"]["url"] == DELIVERY_URL
    assert f"webhook 7: updated url -> {DELIVERY_URL}" in capsys.readouterr().out


def test_repairs_a_hook_registered_with_the_wrong_events(
    repo: FakeRepo, run: Run, github_api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.hooks.append(a_hook(events=("issues", "push")))

    assert run("--webhook-url", TUNNEL) == 0

    assert github_api.only("PATCH", f"/repos/{REPO}/hooks/1").json["events"] == list(
        bootstrap.WEBHOOK_EVENTS
    )
    assert repo.hooks[0]["events"] == list(bootstrap.WEBHOOK_EVENTS)
    assert "events ->" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("hook", "expected"),
    [
        (a_hook(content_type="form"), "content_type -> json"),
        (a_hook(insecure_ssl="1"), "insecure_ssl -> 0"),
        (a_hook(active=False), "active -> true"),
    ],
)
def test_repairs_a_misconfigured_hook(
    repo: FakeRepo,
    run: Run,
    github_api: FakeAPI,
    capsys: pytest.CaptureFixture[str],
    hook: dict[str, Any],
    expected: str,
) -> None:
    repo.hooks.append(hook)

    assert run("--webhook-url", TUNNEL) == 0

    body = github_api.only("PATCH", f"/repos/{REPO}/hooks/1").json
    assert body["active"] is True
    assert body["config"]["content_type"] == "json"
    assert body["config"]["insecure_ssl"] == "0"
    assert expected in capsys.readouterr().out


def test_ignores_a_hook_that_is_not_ours(repo: FakeRepo, run: Run, github_api: FakeAPI) -> None:
    """Recognition is by the receiver path, so somebody else's integration is neither adopted nor
    disturbed."""
    other = a_hook(id=3, url="https://example.com/some/other/receiver", events=("push",))
    repo.hooks.append(json.loads(json.dumps(other)))

    assert run("--webhook-url", TUNNEL) == 0

    assert github_api.sent("PATCH", f"/repos/{REPO}/hooks/3") == []
    assert github_api.only("POST", f"/repos/{REPO}/hooks").json["config"]["url"] == DELIVERY_URL
    assert repo.hooks[0] == other
    assert len(repo.hooks) == 2


def test_does_not_promote_a_dead_hook_beside_a_live_one(
    repo: FakeRepo, run: Run, github_api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    """The precondition is the *expected* first-run state: `docs/09-operations.md#3-webhook` tells
    the operator to `POST` a hook, which creates unconditionally, and the tunnel URL rotates. Two
    or three hooks are waiting for anyone who followed it across a restart.

    Repointing the stale one at the tunnel the working one already uses does not consolidate them.
    It makes both live, and a doubled `check_suite.completed` resumes the same Devin session twice
    — nothing downstream deduplicates a resume, so the ACUs and the fix cycle are spent twice.
    """
    repo.hooks += [
        a_hook(id=4, url="https://dead-tunnel.trycloudflare.com/webhooks/github"),
        a_hook(id=9),
    ]

    assert run("--webhook-url", TUNNEL) == 0

    assert github_api.only("PATCH", f"/repos/{REPO}/hooks/9").json["config"]["url"] == DELIVERY_URL
    assert github_api.sent("PATCH", f"/repos/{REPO}/hooks/4") == []
    assert [hook["config"]["url"] for hook in repo.hooks] == [
        "https://dead-tunnel.trycloudflare.com/webhooks/github",
        DELIVERY_URL,
    ]
    # The one identified for removal is the one that is *not* at the target URL.
    assert "webhook 4 also delivers to this pipeline" in capsys.readouterr().err


def test_keeps_the_oldest_when_none_is_at_the_target_url(
    repo: FakeRepo, run: Run, github_api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two hooks on the same path is somebody's mistake, and guessing which to remove is how a
    re-run becomes unrecoverable. One is moved; the other is named, never deleted."""
    repo.hooks += [
        a_hook(id=4, url="https://old.example/webhooks/github"),
        a_hook(id=9, url="https://older.example/webhooks/github"),
    ]

    assert run("--webhook-url", TUNNEL) == 0

    assert github_api.only("PATCH", f"/repos/{REPO}/hooks/4") is not None
    assert github_api.sent("PATCH", f"/repos/{REPO}/hooks/9") == []
    assert github_api.sent("DELETE") == []
    assert len(repo.hooks) == 2
    assert "webhook 9 also delivers to this pipeline" in capsys.readouterr().err


def test_warns_when_two_hooks_are_both_on_the_target_url(
    repo: FakeRepo, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    """A condition this script will not create but may find. Saying "left in place" understates
    it: until one is removed every event arrives twice."""
    repo.hooks += [a_hook(id=4), a_hook(id=9)]

    assert run("--webhook-url", TUNNEL) == 0

    assert "every event is delivered twice" in capsys.readouterr().err


def test_reconciles_the_hook_where_it_is_when_no_url_is_given(
    repo: FakeRepo, run: Run, github_api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    """`Makefile` runs the script with no arguments and `docs/09-operations.md` never exports
    `$TUNNEL_URL`, so this is the default path — the one a rotated `GITHUB_WEBHOOK_SECRET` has to
    take effect on. Repairing the hook where it stands moves nothing and loses nothing, because a
    `PATCH` replaces `config` wholesale anyway.
    """
    stale = "https://still-running.trycloudflare.com/webhooks/github"
    repo.hooks.append(
        a_hook(id=7, url=stale, events=("issues",), content_type="form", active=False)
    )

    assert run() == 0

    body = github_api.only("PATCH", f"/repos/{REPO}/hooks/7").json
    assert body["config"]["url"] == stale
    assert body["config"]["secret"] == WEBHOOK_SECRET
    assert body["config"]["content_type"] == "json"
    assert body["events"] == list(bootstrap.WEBHOOK_EVENTS)
    assert body["active"] is True
    assert github_api.sent("POST", f"/repos/{REPO}/hooks") == []
    assert repo.hooks[0]["config"]["url"] == stale
    assert f"webhook: reconciling 7 where it is ({stale})" in capsys.readouterr().out


def test_moves_no_hook_when_no_url_is_given(repo: FakeRepo, run: Run, github_api: FakeAPI) -> None:
    """Reconciling without a URL must never repoint anything — that is the whole difference
    between it and a run that was told where the tunnel is."""
    urls = ["https://a.example/webhooks/github", "https://b.example/webhooks/github"]
    repo.hooks += [a_hook(id=4, url=urls[0]), a_hook(id=9, url=urls[1])]

    assert run() == 0

    assert github_api.only("PATCH", f"/repos/{REPO}/hooks/4").json["config"]["url"] == urls[0]
    assert github_api.sent("PATCH", f"/repos/{REPO}/hooks/9") == []
    assert [hook["config"]["url"] for hook in repo.hooks] == urls


def test_says_so_when_no_webhook_is_registered_at_all(
    repo: FakeRepo, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0 with the most important step not done would be a silent failure."""
    assert run() == 0

    captured = capsys.readouterr()
    assert "webhook: not registered" in captured.out
    assert "--webhook-url" in captured.err


# ------------------------------------------------------------------------------ idempotence


def test_a_second_run_creates_nothing(
    repo: FakeRepo, run: Run, github_api: FakeAPI, http_mock: respx.MockRouter
) -> None:
    """The property the whole script exists for, end to end against state that remembers."""
    assert run("--webhook-url", TUNNEL) == 0
    first = len(github_api.requests)

    assert run("--webhook-url", TUNNEL) == 0

    second = github_api.requests[first:]
    assert [request.method for request in second if request.method != "GET"] == ["PATCH"]
    assert [request.path for request in second if request.method == "PATCH"] == [
        f"/repos/{REPO}/hooks/1"
    ]
    assert len(repo.hooks) == 1
    assert [label["name"] for label in repo.labels] == list(EXPECTED_LABELS)
    assert repo.has_issues is True


def test_a_second_run_re_sends_the_secret(
    repo: FakeRepo, run: Run, github_api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    """GitHub masks `config.secret`, so "the hook already has the right secret" is not a knowable
    state: rotating `GITHUB_WEBHOOK_SECRET` has to take effect on a re-run that changes nothing
    else."""
    repo.hooks.append(a_hook(id=5))

    assert run("--webhook-url", TUNNEL) == 0

    assert github_api.only("PATCH", f"/repos/{REPO}/hooks/5").json["config"]["secret"] == (
        WEBHOOK_SECRET
    )
    assert "webhook 5: already current" in capsys.readouterr().out


def test_nothing_is_ever_deleted(repo: FakeRepo, run: Run, github_api: FakeAPI) -> None:
    """Delete-then-recreate is the one shape that turns a re-run into data loss."""
    repo.labels.append({"name": "class:bug", "color": "ededed", "description": "keep me"})
    repo.hooks += [a_hook(id=1, url="https://old.example/webhooks/github"), a_hook(id=2)]

    assert run("--webhook-url", TUNNEL) == 0
    assert run("--webhook-url", TUNNEL) == 0

    assert [request for request in github_api.requests if request.method == "DELETE"] == []
    assert repo.label("class:bug")["description"] == "keep me"
    assert len(repo.hooks) == 2


def test_a_failure_partway_through_leaves_the_repository_usable(
    repo: FakeRepo, run: Run, github_api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    """No step is undone, no hook is registered behind the failure, and the next run completes."""
    repo.broken = ("POST", "/labels")
    repo.broken_after = 2

    assert run("--webhook-url", TUNNEL) == 1

    assert [label["name"] for label in repo.labels] == ["devin:autofix", "needs-human"]
    assert repo.has_issues is True
    assert repo.hooks == []
    assert "bootstrap failed" in capsys.readouterr().err

    repo.broken = None
    assert run("--webhook-url", TUNNEL) == 0

    assert [label["name"] for label in repo.labels] == list(EXPECTED_LABELS)
    assert len(repo.hooks) == 1
    assert len(github_api.sent("POST", f"/repos/{REPO}/labels")) == len(EXPECTED_LABELS) + 1


# ------------------------------------------------------------------- diagnosing a failure


def test_a_403_names_the_permissions_to_check(
    repo: FakeRepo, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    """`docs/09-operations.md` scopes the PAT to issues, pull requests and contents — none of which
    covers `PATCH /repos/{repo}` or the hooks endpoints. GitHub's own message for a fine-grained
    token missing a permission does not say which permission, so the script says it.
    """
    repo.broken = ("PATCH", REPO)
    repo.error_status = 403
    repo.error_body = {"message": "Resource not accessible by personal access token"}

    assert run("--webhook-url", TUNNEL) == 1

    error = capsys.readouterr().err
    assert "Administration: write" in error
    assert "Webhooks: read and write" in error


def test_a_rate_limited_403_is_not_confused_with_a_permission(
    repo: FakeRepo, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    """GitHub uses `403` for both. Told apart by the headers, or the operator goes looking for a
    permission that is already granted."""
    repo.broken = ("PATCH", REPO)
    repo.error_status = 403
    repo.error_body = {"message": "You have exceeded a secondary rate limit"}
    repo.error_headers = {"retry-after": "60", "x-ratelimit-remaining": "0"}

    assert run("--webhook-url", TUNNEL) == 1

    error = capsys.readouterr().err
    assert "rate limited; wait and re-run" in error
    assert "Administration" not in error


def test_a_non_json_response_is_reported_rather_than_raised(
    repo: FakeRepo, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tunnel or proxy in front of GitHub answers `200 text/html`. Left to `response.json()` that
    is a `JSONDecodeError` past `main`'s handler — a traceback instead of the one line telling the
    operator the run is resumable."""
    repo.broken = ("GET", REPO)
    repo.error_status = 200
    repo.error_text = "<html><body>502 Bad Gateway</body></html>"

    assert run("--webhook-url", TUNNEL) == 1

    error = capsys.readouterr().err
    assert "did not answer with JSON" in error
    assert "re-run to complete the remaining steps" in error


# ------------------------------------------------------------------------------ the secret


def test_the_secret_never_reaches_the_output(
    repo: FakeRepo, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.hooks.append(a_hook(id=2, url="https://stale.example/webhooks/github"))

    assert run("--webhook-url", TUNNEL) == 0

    captured = capsys.readouterr()
    assert WEBHOOK_SECRET not in captured.out + captured.err
    assert GITHUB_TOKEN not in captured.out + captured.err


def test_a_rejected_payload_is_reported_without_the_secret(
    repo: FakeRepo, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    """GitHub quotes the payload it rejected back in the `errors` of a `422`, and the payload most
    likely to be rejected here is the one carrying the secret."""
    repo.broken = ("POST", "/hooks")
    repo.error_status = 422
    repo.error_body = {
        "message": "Validation Failed",
        "errors": [{"resource": "Hook", "value": {"secret": WEBHOOK_SECRET}}],
    }

    assert run("--webhook-url", TUNNEL) == 1

    captured = capsys.readouterr()
    assert WEBHOOK_SECRET not in captured.out + captured.err
    assert "[redacted]" in captured.err


def test_the_configuration_error_names_no_values(
    run: Run,
    github_api: FakeAPI,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unconfigured() -> Settings:
        raise ConfigurationError("invalid configuration: GITHUB_TOKEN: Field required")

    monkeypatch.setattr(bootstrap, "get_settings", unconfigured)

    assert bootstrap.main([]) == 2

    assert github_api.requests == []
    assert "GITHUB_TOKEN: Field required" in capsys.readouterr().err


# ------------------------------------------------------------------------------ dry run


def test_a_dry_run_writes_nothing(
    repo: FakeRepo, run: Run, github_api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("--webhook-url", TUNNEL, "--dry-run") == 0

    assert {request.method for request in github_api.requests} == {"GET"}
    assert repo.labels == []
    assert repo.hooks == []
    assert repo.has_issues is False

    out = capsys.readouterr().out
    assert "[dry run] issues: enabled" in out
    assert "[dry run] label devin:autofix: created" in out
    assert f"[dry run] webhook: registered {DELIVERY_URL}" in out
    assert WEBHOOK_SECRET not in out


# ------------------------------------------------------------------------------ authentication


def test_every_request_is_authenticated_and_pinned(
    repo: FakeRepo, run: Run, github_api: FakeAPI
) -> None:
    assert run("--webhook-url", TUNNEL) == 0

    assert github_api.requests
    for request in github_api.requests:
        assert request.headers["authorization"] == f"Bearer {GITHUB_TOKEN}"
        assert request.headers["accept"] == "application/vnd.github+json"
        assert request.headers["x-github-api-version"]
        assert request.headers["user-agent"] == "sentinel-bootstrap"
