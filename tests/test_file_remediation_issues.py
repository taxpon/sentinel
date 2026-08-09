"""`scripts/file_remediation_issues.py` opens eight issues on `taxpon/superset`, a public fork, and
each one starts a Devin session that spends the ACU budget and opens a pull request. So, as with
`tests/test_bootstrap_github.py`, these tests are the whole of the evidence that running it is safe:
it is never run against the real repository with `--apply` to find out.

Three properties carry the weight.

- **A dry run writes nothing.** Asserted as "every request this process made was a `GET`" rather
  than as "no issue was created", because the interesting failure is a write on a path no test
  thought to model. `test_a_dry_run_sends_no_write_of_any_kind` is the one that must never be
  relaxed into a narrower check.
- **A second run files nothing.** The fake fork below holds state and applies the writes it
  receives, so a test can run the script twice and assert on what the *second* run did. A stateless
  mock would answer the second run exactly as it answered the first and prove nothing — the same
  reasoning `tests/test_bootstrap_github.py` records.
- **The issue bodies come from the document.** Everything a filed issue says about Superset has to
  come from `docs/remediation-candidates.md`, because a second copy inside the script would drift
  from T50's triage and the issues would then describe a tree nobody looked at. Asserting the eight
  bodies against eight expected strings *here* would be that second copy, one directory over, so
  the tests instead read the real document and assert that editing it changes what would be filed.

The real `docs/remediation-candidates.md` is loaded rather than a fixture: it is the artefact being
filed, and a fixture would let the two diverge silently — which is the failure mode the whole design
is arranged against.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx

from conftest import GITHUB_API_BASE, GITHUB_TOKEN, FakeAPI, make_settings
from factories import REPO
from sentinel.config import ConfigurationError, Settings
from sentinel.devin.playbooks import IssueClass
from sentinel.observability.logging import REDACTED

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "docs" / "remediation-candidates.md"


def _load(name: str) -> ModuleType:
    """Import a file from `scripts/`, which is not a package and is not on the path.

    Imported rather than run as a subprocess: the subject is the HTTP traffic, and `respx` patches
    transports in this process. The same loader `tests/test_bootstrap_github.py` uses.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed: `@dataclass` resolves its annotations through
    # `sys.modules[cls.__module__]`, and a module not yet there fails at class creation.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


filing = _load("file_remediation_issues")
bootstrap = _load("bootstrap_github")

ISSUES_PATH = f"/repos/{REPO}/issues"
LABELS_PATH = f"/repos/{REPO}/labels"

EXPECTED_CANDIDATES = 8
"""How many targets `docs/remediation-candidates.md` is supposed to carry.

Stated because "eight remediation targets" is the deliverable, not an incidental count: a document
edited down to seven would otherwise file seven issues and pass every other test here.
"""


def document() -> str:
    return DOCUMENT.read_text(encoding="utf-8")


def candidates() -> tuple[Any, ...]:
    return filing.parse(document()).candidates


# ------------------------------------------------------------------------------ the fake fork


@dataclass
class FakeFork:
    """`taxpon/superset` as far as this script can tell: state, and the writes applied to it.

    Only the two collections the script reads are modelled. Issues are appended on `POST`, which is
    what makes the second-run assertions mean anything.
    """

    labels: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    page_size: int | None = None
    broken: tuple[str, str] | None = None
    """`(method, path suffix)` to answer with `error_status`, for the failure tests."""
    error_status: int = 500
    error_body: dict[str, Any] = field(default_factory=lambda: {"message": "Server Error"})
    error_headers: dict[str, str] = field(default_factory=dict)

    _next_number: int = 1

    def install(self, router: respx.MockRouter, repo: str = REPO) -> FakeFork:
        base = f"{GITHUB_API_BASE}/repos/{repo}"
        router.get(f"{base}/issues").mock(side_effect=self._guard(self._list_issues))
        router.post(f"{base}/issues").mock(side_effect=self._guard(self._create_issue))
        router.get(f"{base}/labels").mock(side_effect=self._guard(self._list_labels))
        router.post(f"{base}/labels").mock(side_effect=self._guard(self._create_label))
        return self

    def _guard(self, handler: Any) -> Any:
        def respond(request: httpx.Request) -> httpx.Response:
            if (
                self.broken
                and request.method == self.broken[0]
                and request.url.path.endswith(self.broken[1])
            ):
                return httpx.Response(
                    self.error_status, json=self.error_body, headers=self.error_headers
                )
            return handler(request)

        return respond

    # --- issues ---

    def _list_issues(self, request: httpx.Request) -> httpx.Response:
        return self._page(request, self.issues)

    def _create_issue(self, request: httpx.Request) -> httpx.Response:
        body = _body(request)
        for name in body.get("labels", []):
            if self.label(name) is None:
                # What GitHub does with an unknown label, and the reason `sync_labels` runs first.
                return httpx.Response(
                    422,
                    json={"message": "Validation Failed", "errors": [{"field": "labels"}]},
                )
        issue = {
            "number": self._next_number,
            "title": body["title"],
            "body": body["body"],
            "labels": [{"name": name} for name in body.get("labels", [])],
            "html_url": f"https://github.com/{REPO}/issues/{self._next_number}",
        }
        self._next_number += 1
        self.issues.append(issue)
        return httpx.Response(201, json=issue)

    def titles(self) -> list[str]:
        return [issue["title"] for issue in self.issues]

    # --- labels ---

    def _list_labels(self, request: httpx.Request) -> httpx.Response:
        return self._page(request, self.labels)

    def _create_label(self, request: httpx.Request) -> httpx.Response:
        body = _body(request)
        if self.label(body["name"]) is not None:
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

    def label(self, name: str) -> dict[str, Any] | None:
        """The stored label, matched as GitHub matches it: without regard to case."""
        return next((it for it in self.labels if it["name"].lower() == name.lower()), None)

    def label_names(self) -> list[str]:
        return [label["name"] for label in self.labels]

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


def _body(request: httpx.Request) -> dict[str, Any]:
    body = json.loads(request.content)
    assert isinstance(body, dict)
    return body


def an_issue(title: str, *, number: int = 900, pull_request: bool = False) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "number": number,
        "title": title,
        "body": "",
        "labels": [],
        "html_url": f"https://github.com/{REPO}/issues/{number}",
    }
    if pull_request:
        issue["pull_request"] = {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}
    return issue


# ------------------------------------------------------------------------------ running it


Run = Any


@pytest.fixture
def fork(http_mock: respx.MockRouter) -> FakeFork:
    """A fresh fork: no labels of ours, no issues."""
    return FakeFork().install(http_mock)


@pytest.fixture
def run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Run:
    """Invoke the script in-process, always against the real document unless told otherwise."""
    monkeypatch.setattr(filing, "get_settings", lambda: settings)

    def invoke(*argv: str, config: Settings | None = None) -> int:
        if config is not None:
            monkeypatch.setattr(filing, "get_settings", lambda: config)
        if not any(arg.startswith("--document") for arg in argv):
            argv = (*argv, "--document", str(DOCUMENT))
        return int(filing.main(list(argv)))

    return invoke


# ------------------------------------------------------------- the dry run writes nothing


def test_a_dry_run_sends_no_write_of_any_kind(
    fork: FakeFork, run: Run, github_api: FakeAPI
) -> None:
    """The property the whole script is arranged around, asserted at its widest.

    Not "no issue was created" and not "no label was created": *no request that is not a read*. A
    narrower assertion would pass a version of this script that had grown a fourth endpoint, which
    is exactly the change that would need catching.
    """
    assert run() == 0

    assert [request.method for request in github_api.requests] == ["GET", "GET"]
    assert fork.issues == []
    assert fork.labels == []


def test_a_dry_run_is_the_default_with_no_arguments(
    fork: FakeFork, run: Run, github_api: FakeAPI
) -> None:
    """`--apply` is the only thing that writes. Neither bare invocation nor `--dry-run` does."""
    assert run() == 0
    assert run("--dry-run") == 0

    assert github_api.sent("POST") == []


def test_apply_and_dry_run_cannot_be_given_together(run: Run) -> None:
    with pytest.raises(SystemExit):
        run("--apply", "--dry-run")


def test_a_dry_run_reports_what_it_would_file(
    fork: FakeFork, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run() == 0

    out = capsys.readouterr().out
    assert "dry run — nothing is written" in out
    assert f"{EXPECTED_CANDIDATES} issue(s) would be filed" in out
    # Every reported action is marked as one that did not happen.
    for line in out.splitlines():
        if " filed: " in line or " created" in line:
            assert line.lstrip().startswith("[dry run]"), line


# ------------------------------------------------------------------------ filing, under --apply


def test_apply_files_one_issue_per_candidate(fork: FakeFork, run: Run, github_api: FakeAPI) -> None:
    assert run("--apply") == 0

    filed = github_api.sent("POST", ISSUES_PATH)
    assert len(filed) == EXPECTED_CANDIDATES == len(candidates())
    assert fork.titles() == [filing.issue_title(c) for c in candidates()]


def test_every_filed_issue_carries_the_trigger_label(
    fork: FakeFork, run: Run, github_api: FakeAPI, settings: Settings
) -> None:
    """The label `sentinel.github.events` dispatches on, plus the one that selects the playbook.

    An issue filed without the trigger label is a remediation that never starts and reports nothing
    — the failure is silence, so it is asserted on the captured request body rather than inferred.
    """
    assert run("--apply") == 0

    for request, candidate in zip(github_api.sent("POST", ISSUES_PATH), candidates(), strict=True):
        assert request.json["labels"] == [
            settings.autofix_label,
            f"class:{candidate.issue_class}",
        ]


def test_the_trigger_label_is_whatever_the_configuration_says(
    fork: FakeFork, run: Run, github_api: FakeAPI
) -> None:
    """`AUTOFIX_LABEL` is configurable, and the pipeline watches the configured one.

    Hardcoding `devin:autofix` here would let the script and `sentinel.config` drift apart in the
    one direction that files eight issues nothing is listening for.
    """
    renamed = make_settings(autofix_label="ops:fix-it")

    assert run("--apply", config=renamed) == 0

    assert "ops:fix-it" in fork.label_names()
    assert "devin:autofix" not in fork.label_names()
    for request in github_api.sent("POST", ISSUES_PATH):
        assert request.json["labels"][0] == "ops:fix-it"


def test_missing_labels_are_created_before_the_issues_that_use_them(
    fork: FakeFork, run: Run, github_api: FakeAPI, settings: Settings
) -> None:
    """A fork nobody bootstrapped still gets eight issues that dispatch, not eight 422s."""
    assert run("--apply") == 0

    created = [request.json for request in github_api.sent("POST", LABELS_PATH)]
    assert created[0] == {
        "name": settings.autofix_label,
        "color": filing.TRIGGER_LABEL_COLOR,
        "description": filing.TRIGGER_LABEL_DESCRIPTION,
    }
    assert [label["name"] for label in created[1:]] == [
        f"class:{candidate.issue_class}" for candidate in candidates()
    ]
    assert all(label["color"] == filing.CLASS_LABEL_COLOR for label in created[1:])

    methods = [(r.method, r.path) for r in github_api.requests]
    assert methods.index(("POST", LABELS_PATH)) < methods.index(("POST", ISSUES_PATH))


def test_labels_the_fork_already_has_are_left_alone(
    fork: FakeFork, run: Run, github_api: FakeAPI, settings: Settings
) -> None:
    """Existing means existing — nothing is patched here, and case is GitHub's, not Python's."""
    fork.labels = [
        {"name": settings.autofix_label.upper(), "color": "ffffff", "description": "theirs"},
        {"name": "class:security", "color": "ffffff", "description": None},
    ]

    assert run("--apply") == 0

    created = [request.json["name"] for request in github_api.sent("POST", LABELS_PATH)]
    assert settings.autofix_label not in created
    assert "class:security" not in created
    assert fork.label(settings.autofix_label) == {
        "name": settings.autofix_label.upper(),
        "color": "ffffff",
        "description": "theirs",
    }


def test_the_label_fields_agree_with_the_bootstrap_script(settings: Settings) -> None:
    """The two scripts create the same labels, and `scripts/` cannot import across itself.

    `scripts/file_remediation_issues.py` restates the colours rather than importing them, because
    a cross-script import would need the script's own directory on `sys.path` — true when it is
    executed, false when it is loaded here. This is the assertion that pays for that restatement.
    """
    by_name = {label.name: label for label in bootstrap.desired_labels(settings)}
    trigger = by_name[settings.autofix_label]
    assert (trigger.color, trigger.description) == (
        filing.TRIGGER_LABEL_COLOR,
        filing.TRIGGER_LABEL_DESCRIPTION,
    )
    assert by_name[f"class:{IssueClass.SECURITY}"].color == filing.CLASS_LABEL_COLOR


# ------------------------------------------------------------------------------ idempotence


def test_a_second_apply_files_nothing(fork: FakeFork, run: Run, github_api: FakeAPI) -> None:
    """The one that matters: `--apply` twice must not double-file.

    The fake fork keeps what the first run created, so the second run reads back its own writes —
    which is the arrangement the real repository presents on a re-run.
    """
    assert run("--apply") == 0
    first = len(github_api.sent("POST", ISSUES_PATH))

    assert run("--apply") == 0

    assert len(github_api.sent("POST", ISSUES_PATH)) == first == EXPECTED_CANDIDATES
    assert len(fork.issues) == EXPECTED_CANDIDATES


def test_a_candidate_already_filed_is_skipped_by_title(
    fork: FakeFork, run: Run, github_api: FakeAPI
) -> None:
    already = filing.issue_title(candidates()[2])
    fork.issues = [an_issue(already)]

    assert run("--apply") == 0

    assert already not in [
        request.json["title"] for request in github_api.sent("POST", ISSUES_PATH)
    ]
    assert len(github_api.sent("POST", ISSUES_PATH)) == EXPECTED_CANDIDATES - 1


def test_the_dry_run_names_what_it_would_skip(
    fork: FakeFork, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator has to be able to read off which of the eight are already there before typing
    `--apply`, otherwise the only way to find out is to run it."""
    candidate = candidates()[0]
    fork.issues = [an_issue(filing.issue_title(candidate))]

    assert run() == 0

    out = capsys.readouterr().out
    assert f"{candidate.id} skipped" in out
    assert filing.issue_title(candidate) in out
    assert f"{EXPECTED_CANDIDATES - 1} issue(s) would be filed" in out


def test_a_closed_issue_still_counts_as_filed(
    fork: FakeFork, run: Run, github_api: FakeAPI
) -> None:
    """Somebody closed it, which is a decision. Re-filing would overrule it silently."""
    closed = filing.issue_title(candidates()[1])
    fork.issues = [{**an_issue(closed), "state": "closed"}]

    assert run("--apply") == 0

    assert closed not in [r.json["title"] for r in github_api.sent("POST", ISSUES_PATH)]
    assert github_api.sent("GET", ISSUES_PATH)[0].url.params["state"] == "all"


def test_a_pull_request_with_the_same_title_does_not_count(
    fork: FakeFork, run: Run, github_api: FakeAPI
) -> None:
    """`GET /issues` returns pull requests too, and Sentinel's own remediation pull request takes
    its title from the issue it closes. Counting one would skip a candidate never filed."""
    candidate = candidates()[0]
    fork.issues = [an_issue(filing.issue_title(candidate), pull_request=True)]

    assert run("--apply") == 0

    assert filing.issue_title(candidate) in [
        request.json["title"] for request in github_api.sent("POST", ISSUES_PATH)
    ]
    assert len(github_api.sent("POST", ISSUES_PATH)) == EXPECTED_CANDIDATES


def test_existing_issues_are_read_past_the_first_page(
    fork: FakeFork, run: Run, github_api: FakeAPI
) -> None:
    """An issue list read one page deep makes a filed issue look absent — the duplicate this
    script exists to avoid."""
    already = filing.issue_title(candidates()[-1])
    fork.issues = [an_issue(f"unrelated #{n}", number=n) for n in range(1, 12)]
    fork.issues.append(an_issue(already, number=99))
    fork.page_size = 5

    assert run("--apply") == 0

    assert len(github_api.sent("GET", ISSUES_PATH)) == 3
    assert already not in [r.json["title"] for r in github_api.sent("POST", ISSUES_PATH)]


def test_labels_are_read_past_the_first_page(
    fork: FakeFork, run: Run, github_api: FakeAPI, settings: Settings
) -> None:
    fork.labels = [{"name": f"inherited-{n}", "color": "ededed"} for n in range(12)]
    fork.labels.append({"name": settings.autofix_label, "color": filing.TRIGGER_LABEL_COLOR})
    fork.page_size = 5

    assert run("--apply") == 0

    created = [request.json["name"] for request in github_api.sent("POST", LABELS_PATH)]
    assert settings.autofix_label not in created


# ------------------------------------------------------- the bodies come from the document


def test_every_candidate_in_the_document_is_parsed() -> None:
    """Eight targets, eight distinct classes — the deliverable `docs/remediation-candidates.md`
    describes, read back off the document itself."""
    parsed = candidates()

    assert len(parsed) == EXPECTED_CANDIDATES
    assert [candidate.id for candidate in parsed] == [
        f"C{n}" for n in range(1, EXPECTED_CANDIDATES + 1)
    ]
    assert {candidate.issue_class for candidate in parsed} == set(IssueClass)


def test_the_body_is_rebuilt_from_the_document_rather_than_stored(
    tmp_path: Path, settings: Settings
) -> None:
    """The anti-drift assertion, and the reason no expected body is written out in this file.

    An edit to the document has to reach the issue. Asserting eight bodies against eight literals
    here would be the second copy the script deliberately avoids, one directory over, and it would
    drift from T50's triage in exactly the same way.
    """
    edited = document().replace(
        "The `exp.Command` fallback is evaluated for every dialect",
        "PLACEHOLDER SENTENCE FOR THE TEST",
    )
    assert edited != document(), "the sentence this test edits has moved; update it"
    altered = tmp_path / "candidates.md"
    altered.write_text(edited, encoding="utf-8")

    triage = filing.parse(altered.read_text(encoding="utf-8"))
    body = filing.issue_body(triage.candidates[0], triage, settings)

    assert "PLACEHOLDER SENTENCE FOR THE TEST" in body
    assert "The `exp.Command` fallback is evaluated for every dialect" not in body


def test_the_script_restates_nothing_the_document_says_about_superset() -> None:
    """No candidate's own words appear in the script's source.

    The mapping is a parse, not a table. If a title or a file path from the document turns up in
    `scripts/file_remediation_issues.py`, somebody has started keeping a second copy and the two
    will come apart at the next edit to the triage.
    """
    source = (ROOT / "scripts" / "file_remediation_issues.py").read_text(encoding="utf-8")

    for candidate in candidates():
        assert candidate.title not in source
        for section in candidate.sections.values():
            assert section not in source
    for superset_detail in ("superset/sql/parse.py", "paramiko", "deck.gl", "TagDAO", "sqlglot"):
        assert superset_detail not in source


@pytest.mark.parametrize("index", range(EXPECTED_CANDIDATES))
def test_each_body_is_self_contained(index: int, settings: Settings) -> None:
    """What an agent that has read nothing else needs: what is wrong, where, how to verify, and
    what "done" looks like. Every candidate, not a sampled one."""
    triage = filing.parse(document())
    candidate = triage.candidates[index]
    body = filing.issue_body(candidate, triage, settings)

    for heading in ("What is wrong", 'What "fixed" means', "The regression test", "Blast radius"):
        assert f"## {heading}" in body
    assert "## Definition of done" in body
    assert f"| Class | `{candidate.issue_class}` |" in body
    assert candidate.id in body
    # The substance, not just the scaffolding: the document's own evidence text is carried over.
    assert candidate.sections["Evidence"].splitlines()[0][:60] in body


def test_every_body_states_which_checkout_the_evidence_was_read_on(settings: Settings) -> None:
    """The row that was silently missing until the provenance pattern was made whitespace-tolerant.

    The document is hard-wrapped, and the sentence naming the checkout currently breaks mid-phrase.
    A pattern with a literal space matched nothing, `_provenance` returned `None`, and the row was
    simply left out — so every issue quoted file-and-line evidence with no statement of which
    commit it was true of. Re-wrapping that paragraph fails here rather than on the fork.
    """
    triage = filing.parse(document())

    assert triage.provenance is not None
    for candidate in triage.candidates:
        body = filing.issue_body(candidate, triage, settings)
        assert f"| Evidence read on | {triage.provenance} |" in body
        assert "f5bca3b" in body


def test_the_caveats_reach_the_candidates_they_name(settings: Settings) -> None:
    """ "What could not be verified" is the document's record of the limits of its own evidence.

    C2's fixed version was *inferred* rather than published. An agent will otherwise take it on
    trust, which is the one thing that section exists to prevent.
    """
    triage = filing.parse(document())
    by_id = {candidate.id: candidate for candidate in triage.candidates}

    c2 = filing.issue_body(by_id["C2"], triage, settings)
    assert "## What could not be verified" in c2
    assert "paramiko 5.0.0 is the fixed version" in c2

    # A bullet naming no candidate belongs to the set, not to an issue.
    assert "31 published" not in c2
    # And a caveat about another candidate does not leak in.
    assert "104810" not in c2
    assert "104810" in filing.issue_body(by_id["C3"], triage, settings)


def test_the_portfolio_rationale_is_not_filed(settings: Settings) -> None:
    """ "Why it is a good target" is written for the reviewer of the selection — which candidate is
    strongest, what capability each demonstrates. It belongs to choosing the eight, and telling an
    agent its task was picked because it is hard is not information it can act on."""
    triage = filing.parse(document())

    for candidate in triage.candidates:
        body = filing.issue_body(candidate, triage, settings)
        assert "Why it is a good target" not in body
        assert "portfolio" not in body.lower()


def test_links_into_sentinels_own_repository_are_not_left_dangling(settings: Settings) -> None:
    """The issue is read on `taxpon/superset`, where a relative link resolves to a file that is not
    there. Absolute links — an upstream issue, an advisory — are exactly what should be followed."""
    triage = filing.parse(document())
    by_id = {candidate.id: candidate for candidate in triage.candidates}

    body = filing.issue_body(by_id["C3"], triage, settings)
    assert "](./08-testing.md)" not in body
    assert "`docs/08-testing.md`" in body
    assert "https://github.com/apache/superset/issues/40871" in body


def test_the_title_carries_the_class_and_no_markup() -> None:
    """A GitHub issue title is plain text and renders backticks literally. It is also the key the
    skip is done on, so it has to be something a human would type the same way twice."""
    for candidate in candidates():
        title = filing.issue_title(candidate)
        assert title.startswith(f"{candidate.issue_class}: ")
        assert "`" not in title

    assert "flaky-test: Skipped" in [filing.issue_title(c) for c in candidates()][4]


def test_the_definition_of_done_targets_the_configured_base_branch(settings: Settings) -> None:
    """`master`, not `main` — the fork's default branch. A pull request opened against a branch
    that does not exist is a remediation that produces nothing."""
    triage = filing.parse(document())

    body = filing.issue_body(triage.candidates[0], triage, settings)
    assert f"The pull request targets `{settings.target_base_branch}`" in body
    assert settings.target_base_branch == "master"


# --------------------------------------------------------------------- refusing to file rubbish


@pytest.mark.parametrize(
    ("old", "new", "count", "expected"),
    [
        pytest.param("**Class:** `security`\n", "", 1, "does not declare", id="no-class"),
        pytest.param("**Class:** `security`", "**Class:** `wat`", 1, "unknown issue", id="bad"),
        pytest.param("### Blast radius", "### Blast radii", 1, "Blast radius", id="no-section"),
        pytest.param("\n## C", "\n## D", -1, "no candidates found", id="no-candidates"),
    ],
)
def test_a_document_that_cannot_be_read_files_nothing(
    old: str,
    new: str,
    count: int,
    expected: str,
    tmp_path: Path,
    fork: FakeFork,
    run: Run,
    github_api: FakeAPI,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fatal, never skipped, and before any request is sent.

    An issue assembled from a document the parser could not read would be filed on a public
    repository and handed to an agent with nothing else to go on. Failing at the fork is hours too
    late, so the whole run refuses.
    """
    text = document().replace(old, new, count)
    assert text != document(), "the text this case edits has moved; update it"
    broken = tmp_path / "candidates.md"
    broken.write_text(text, encoding="utf-8")

    assert run("--apply", "--document", str(broken)) == filing.EXIT_UNREADABLE_DOCUMENT

    assert expected in capsys.readouterr().err
    assert github_api.requests == []
    assert fork.issues == []


def test_a_class_that_disagrees_with_the_summary_table_is_refused(tmp_path: Path) -> None:
    """The class selects the playbook and the ACU cap, so it cannot be both."""
    text = document().replace("**Class:** `perf`", "**Class:** `bug`", 1)

    with pytest.raises(filing.DocumentError, match="cannot be both"):
        filing.parse(text)


def test_a_document_that_is_not_there_files_nothing(
    fork: FakeFork, run: Run, github_api: FakeAPI, tmp_path: Path
) -> None:
    assert run("--apply", "--document", str(tmp_path / "nope.md")) == (
        filing.EXIT_UNREADABLE_DOCUMENT
    )
    assert github_api.requests == []


def test_a_misconfigured_environment_files_nothing(
    fork: FakeFork,
    run: Run,
    github_api: FakeAPI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unconfigured() -> Settings:
        raise ConfigurationError("GITHUB_TOKEN: Field required")

    monkeypatch.setattr(filing, "get_settings", unconfigured)

    assert run("--apply") == filing.EXIT_MISCONFIGURED

    assert "GITHUB_TOKEN" in capsys.readouterr().err
    assert github_api.requests == []


# ------------------------------------------------------------------------------ failing mid-run


def test_a_failure_stops_the_run_and_says_what_stands(
    fork: FakeFork, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing is undone. The next run files the rest, because the skip is by title."""
    fork.broken = ("POST", "/issues")

    assert run("--apply") == filing.EXIT_STEP_FAILED

    err = capsys.readouterr().err
    assert "filing failed" in err
    assert "re-run to file the rest" in err


def test_a_switched_off_issue_tracker_says_so(
    fork: FakeFork, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    """B1: the fork's tracker starts disabled, and `GET /issues` answers `410`. The message has to
    point at the bootstrap rather than read as an unexplained failure."""
    fork.broken = ("GET", "/issues")
    fork.error_status = 410
    fork.error_body = {"message": "Issues are disabled for this repo"}

    assert run("--apply") == filing.EXIT_STEP_FAILED

    assert "make bootstrap-github" in capsys.readouterr().err


def test_rate_limiting_is_named_as_such(
    fork: FakeFork, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    fork.broken = ("GET", "/labels")
    fork.error_status = 403
    fork.error_headers = {"x-ratelimit-remaining": "0"}

    assert run("--apply") == filing.EXIT_STEP_FAILED

    assert "rate limited" in capsys.readouterr().err


# ------------------------------------------------------------------------------ the token


def test_the_token_is_sent_to_github_and_nowhere_else(
    fork: FakeFork, run: Run, github_api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("--apply") == 0

    for request in github_api.requests:
        assert request.headers["authorization"] == f"Bearer {GITHUB_TOKEN}"
    captured = capsys.readouterr()
    assert GITHUB_TOKEN not in captured.out
    assert GITHUB_TOKEN not in captured.err


def test_a_rejected_payload_echoed_back_does_not_print_the_token(
    fork: FakeFork, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    """GitHub echoes a rejected payload back in the `errors` of a `422`, and an error message is
    the one place a script that prints could otherwise surface a credential."""
    fork.broken = ("POST", "/issues")
    fork.error_status = 422
    fork.error_body = {"message": "Validation Failed", "errors": [{"token": GITHUB_TOKEN}]}

    assert run("--apply") == filing.EXIT_STEP_FAILED

    captured = capsys.readouterr()
    assert GITHUB_TOKEN not in captured.out + captured.err
    assert REDACTED in captured.err


def test_the_repr_of_the_fork_names_the_repository_not_the_credentials(
    settings: Settings,
) -> None:
    with httpx.Client(base_url=GITHUB_API_BASE) as http:
        fork = filing.Fork(http, settings.target_repo, secrets=frozenset(), dry_run=True)
        assert repr(fork) == f"Fork(repo={settings.target_repo!r}, dry_run=True)"
