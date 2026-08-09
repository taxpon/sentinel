"""File the remediation targets of `docs/remediation-candidates.md` as issues on the fork.

    uv run scripts/file_remediation_issues.py                # dry run — the default
    uv run scripts/file_remediation_issues.py --show-bodies  # dry run, with the full issue bodies
    uv run scripts/file_remediation_issues.py --apply        # actually file them

`GITHUB_TOKEN` is the only variable this reads, in either mode: it loads `GitHubSettings` rather
than the whole `Settings`, so filing issues on the fork does not wait on Devin credentials or a
database (`docs/adr/2026-08-10-a-script-loads-the-configuration-group-it-reads.md`).

**A dry run is what this script does unless it is told otherwise.** Filing eight issues on a public
fork is not a step to discover you have taken: it starts eight Devin sessions, spends the ACU budget
and opens eight pull requests on a repository other people can see. So `--apply` is a word an
operator has to type, and everything short of it makes no request that could change anything.

**Idempotent by title.** The fork's issues are listed first — open and closed — and a candidate
whose title is already there is skipped rather than filed again, the same shape
`scripts/seed_issues.py` uses. A second `--apply` therefore files nothing. Titles are stable because
they are derived from the document's own headings, so this holds across runs as long as the
headings hold.

**The document is the only source of what an issue says.** The eight candidates are parsed out of
`docs/remediation-candidates.md` — heading, class, evidence, what "fixed" means, the regression
test, the blast radius and the caveats recorded under "What could not be verified". Nothing about
Superset is restated here, because a second copy would drift from T50's and the issue would then
describe a tree nobody triaged. What this file does add is the part the document does not carry:
the definition of done, which is about how the fork's CI works rather than about any one defect.

What is deliberately *not* copied is each candidate's "Why it is a good target" section. It is
portfolio rationale written for the reviewer of the selection — which candidate is strongest, what
capability each one demonstrates — and it belongs to choosing the eight rather than to fixing one.

The trigger label comes from `settings.autofix_label` rather than from a constant here, for the
reason `scripts/bootstrap_github.py` gives: an issue filed under a label the pipeline does not watch
is a remediation that never starts and reports nothing. The labels the issues carry are reconciled
onto the fork first, exactly as that script does, so a fork that has not been bootstrapped does not
end up with eight issues carrying a label it cannot express.

Unlike `scripts/seed_issues.py` and `scripts/gen_adr_index.py`, this file carries no PEP 723 header.
It imports `sentinel`, which `uv run --script` resolves in an isolated environment that does not
contain the project; without the header, `uv run scripts/file_remediation_issues.py` runs in the
project environment, against the versions `uv.lock` pins. That is the same trade
`scripts/bootstrap_devin.py` and `scripts/bootstrap_github.py` record.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx

from sentinel.config import ConfigurationError, GitHubSettings, TargetSettings, load_config
from sentinel.devin.playbooks import IssueClass, UnknownIssueClass, parse_issue_class
from sentinel.github.client import API_VERSION, GITHUB_API_BASE
from sentinel.observability.logging import REDACTED, secret_values

CANDIDATES = Path("docs/remediation-candidates.md")

TRIGGER_LABEL_COLOR: Final = "0e8a16"
TRIGGER_LABEL_DESCRIPTION: Final = "Sentinel: remediate automatically"
CLASS_LABEL_PREFIX: Final = "class:"
CLASS_LABEL_COLOR: Final = "5319e7"
"""The label fields `scripts/bootstrap_github.py` creates these labels with.

Restated rather than imported: `scripts/` is not a package, so a cross-script import would depend on
the script's own directory being on `sys.path`, which is true when the file is executed and false
when the tests load it. `tests/test_file_remediation_issues.py` asserts the two agree instead, so a
colour changed in one place fails there rather than on the fork."""

REQUEST_TIMEOUT_SECONDS: Final = 30.0
PAGE_SIZE: Final = 100
BODY_EXCERPT_CHARS: Final = 400

EXIT_STEP_FAILED: Final = 1
EXIT_MISCONFIGURED: Final = 2
EXIT_UNREADABLE_DOCUMENT: Final = 3


class FilingError(RuntimeError):
    """A step did not complete. Whatever was filed before it stands; re-running files the rest."""


class DocumentError(RuntimeError):
    """`docs/remediation-candidates.md` does not carry what an issue needs.

    Raised before anything is sent, because an issue body assembled from a document this could not
    read would be filed on a public repository and read by an agent that has nothing else to go on.
    """


# --- The document ---------------------------------------------------------------------------------

REQUIRED_SECTIONS: Final[tuple[str, ...]] = (
    "Evidence",
    "Fixed means",
    "Regression test",
    "Blast radius",
)
"""The subsections every candidate must carry, and everything an issue body is built from."""

_SECTION: Final = re.compile(r"^## (?P<heading>.+)$", re.MULTILINE)
_SUBSECTION: Final = re.compile(r"^### (?P<heading>.+)$", re.MULTILINE)
_ANCHOR: Final = re.compile(r"\s*\{#[^}]+\}\s*$")
_CANDIDATE = re.compile(r"^(?P<id>C\d+)\s+—\s+(?P<title>.+)$")
_CLASS: Final = re.compile(r"^\*\*Class:\*\*\s*`(?P<value>[^`]+)`\s*$", re.MULTILINE)
_SUMMARY_ROW: Final = re.compile(
    r"^\|\s*\[(?P<id>C\d+)\]\([^)]*\)\s*\|[^|]*\|\s*`(?P<class>[^`]+)`\s*\|(?P<strength>[^|]*)\|$",
    re.MULTILINE,
)
_PROVENANCE: Final = re.compile(
    r"working\s+checkout\s+was\s+`(?P<repo>[^`]+)`\s+at\s+`(?P<commit>[0-9a-f]{7,40})`"
    r"(?:\s*(?P<note>\([^)]*\)))?"
)
"""Where the evidence was read, from the document's "How these were selected" prose.

Every gap is `\\s+` rather than a literal space because the document is hard-wrapped at 100 columns
and the sentence currently breaks between "The working" and "checkout was". A literal space matched
nothing, and the failure was silent in the worst way: `_provenance` returns `None`, the row is
simply left out, and every issue went out claiming line numbers with no statement of which commit
they were true of. `tests/test_file_remediation_issues.py` asserts the row is present in a rendered
body, so a re-wrap of that paragraph fails there rather than on the fork."""
_UNVERIFIED: Final = "What could not be verified"
_BULLET: Final = re.compile(r"^-\s+(?P<text>.*)$")
_CAVEAT_OF: Final = re.compile(r"\s*\((?P<ids>C\d+(?:\s*,\s*C\d+)*)\)\s*$")
_CANDIDATE_ID: Final = re.compile(r"C\d+")

_RELATIVE_LINK: Final = re.compile(r"\[[^\]]*\]\((?!https?://)(?P<path>[^)]+)\)")
"""A link into Sentinel's own repository, which the fork cannot resolve. See `_delink`."""

_BACKTICK: Final = re.compile(r"`+")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One remediation target, as `docs/remediation-candidates.md` states it."""

    id: str
    title: str
    issue_class: IssueClass
    evidence_strength: str
    sections: Mapping[str, str]
    caveats: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Triage:
    """What was parsed out of the document: the candidates, and where the evidence came from."""

    candidates: tuple[Candidate, ...]
    provenance: str | None
    """The checkout the evidence was read on, as the document states it. Carried into every issue:
    the fork moves on, and a reader has to know that the line numbers below were true of a commit
    rather than of `master` today."""


def _chunks(text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """Each heading matched by `pattern` with the text under it, up to the next such heading."""
    headings = list(pattern.finditer(text))
    return [
        (
            _ANCHOR.sub("", match.group("heading")).strip(),
            _trimmed(
                text[
                    match.end() : headings[index + 1].start()
                    if index + 1 < len(headings)
                    else len(text)
                ]
            ),
        )
        for index, match in enumerate(headings)
    ]


def _trimmed(body: str) -> str:
    """A section's text without the horizontal rule the document separates candidates with."""
    return body.strip().removesuffix("---").strip()


def _bullets(section: str) -> Iterator[str]:
    """Each `-` item of a section as one line, with its wrapped continuations folded back in."""
    current: list[str] = []
    for line in section.splitlines():
        item = _BULLET.match(line)
        if item is not None:
            if current:
                yield " ".join(current)
            current = [item.group("text").strip()]
        elif not line.strip():
            if current:
                yield " ".join(current)
            current = []
        elif current:
            current.append(line.strip())
    if current:
        yield " ".join(current)


def _caveats(text: str) -> Mapping[str, tuple[str, ...]]:
    """The "What could not be verified" bullets, indexed by the candidate each one names.

    These matter more than their length suggests. A candidate whose fixed version was *inferred*
    rather than published (C2) is one an agent will otherwise take on trust, and the whole point of
    the document recording the limits of its own evidence is lost if the issue does not carry them.
    A bullet naming no candidate — there is one, about Superset's published advisories as a whole —
    belongs to the set rather than to an issue, and is left here.
    """
    section = next(
        (body for heading, body in _chunks(text, _SECTION) if heading.startswith(_UNVERIFIED)),
        "",
    )
    found: dict[str, list[str]] = {}
    for bullet in _bullets(section):
        tagged = _CAVEAT_OF.search(bullet)
        if tagged is None:
            continue
        for identifier in _CANDIDATE_ID.findall(tagged.group("ids")):
            found.setdefault(identifier, []).append(_CAVEAT_OF.sub("", bullet))
    return {identifier: tuple(items) for identifier, items in found.items()}


def parse(text: str) -> Triage:
    """Read the document into the candidates it describes, or refuse to.

    Every mismatch is fatal rather than skipped. A candidate missing its regression test, or filed
    under a class no playbook knows, produces an issue that reaches a Devin session and cannot be
    acted on — and the failure would surface on the fork, hours later, as a session with nothing to
    do.
    """
    summary = {
        match.group("id"): (match.group("class"), _collapse(match.group("strength")))
        for match in _SUMMARY_ROW.finditer(text)
    }
    caveats = _caveats(text)

    candidates: list[Candidate] = []
    for heading, body in _chunks(text, _SECTION):
        named = _CANDIDATE.match(heading)
        if named is None:
            continue
        identifier = named.group("id")
        declared = _CLASS.search(body)
        if declared is None:
            raise DocumentError(f"{identifier} does not declare a **Class:**")
        try:
            issue_class = parse_issue_class(declared.group("value"))
        except UnknownIssueClass as exc:
            raise DocumentError(f"{identifier}: {exc}") from None

        sections = dict(_chunks(body, _SUBSECTION))
        missing = [name for name in REQUIRED_SECTIONS if not sections.get(name)]
        if missing:
            raise DocumentError(f"{identifier} has no {', '.join(missing)} section")

        listed, strength = summary.get(identifier, (None, ""))
        if listed is not None and listed != issue_class.value:
            raise DocumentError(
                f"{identifier} is `{issue_class}` in its own section and `{listed}` in the summary "
                "table; the class selects the playbook and the ACU cap, so it cannot be both"
            )
        candidates.append(
            Candidate(
                id=identifier,
                title=_collapse(named.group("title")),
                issue_class=issue_class,
                evidence_strength=strength,
                sections=sections,
                caveats=caveats.get(identifier, ()),
            )
        )

    if not candidates:
        raise DocumentError("no candidates found — every one is a `## C<n> — <title>` heading")
    return Triage(candidates=tuple(candidates), provenance=_provenance(text))


def _provenance(text: str) -> str | None:
    """ "`<repo>` at `<commit>` (<note>)", or `None` if the document does not say."""
    found = _PROVENANCE.search(text)
    if found is None:
        return None
    note = found.group("note")
    return f"`{found.group('repo')}` at `{found.group('commit')}`" + (f" {note}" if note else "")


def _collapse(text: str) -> str:
    return " ".join(text.split()).strip()


# --- The issue ------------------------------------------------------------------------------------

BODY_SECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("Evidence", "What is wrong"),
    ("Fixed means", 'What "fixed" means'),
    ("Regression test", "The regression test"),
    ("Blast radius", "Blast radius"),
)
"""Which section of the document becomes which heading of the issue. The document is written for
somebody choosing between candidates; the issue is written for somebody fixing one, so "Evidence"
becomes "What is wrong" rather than being copied under a heading that invites a second opinion."""

DEFINITION_OF_DONE: Final = """\
- [ ] The defect above is fixed, and the pull request explains the **root cause** — why the defect
      exists — rather than restating the diff.
- [ ] A regression test exists at the host named above. It **fails on the current tree and passes
      with the fix**; the pull request quotes the exact command and what it printed both times.
- [ ] That test is somewhere this repository's CI actually runs: under `tests/unit_tests/**` for
      Python, or in a jest suite belonging to a frontend package the diff touches. CI runs
      `pre-commit` over the changed files, `pytest` scoped to the test paths the diff touches, and
      `npm test` scoped to the changed frontend packages — integration and end-to-end suites do not
      run at all, so a test placed in one proves nothing.
- [ ] No lint or type rule was silenced to get past the gate: no new `# noqa`, no new
      `# type: ignore`, no path added to a tool's exclude list.
- [ ] Nothing unrelated was folded in — no drive-by cleanup, no formatting pass, no dependency bump
      the fix does not require. One pull request does one thing.
- [ ] The pull request targets `{branch}`.
- [ ] If investigation shows the description above is wrong, report `outcome: blocked` with what
      you found instead, rather than opening a pull request for something else. That is a useful
      answer."""
"""What "done" means, which is the one thing the document does not state per candidate.

It is about how this fork's CI is narrowed rather than about any single defect, so it is written
here rather than parsed: `docs/remediation-candidates.md` states the narrowing once, in prose, as
the constraint that shaped the whole selection."""


def issue_title(candidate: Candidate) -> str:
    """`<class>: <the document's own heading>`.

    The class prefix is the shape `sentinel.scanner.audit` already files issues under, so the two
    sources of remediation issues read alike in the fork's issue list. Backticks are dropped: a
    GitHub issue title is plain text and renders them literally, and the title is also the key this
    script skips on, so it has to be something a human would type the same way twice.
    """
    return f"{candidate.issue_class}: {_BACKTICK.sub('', candidate.title)}"


def issue_labels(candidate: Candidate, settings: TargetSettings) -> tuple[str, ...]:
    """The trigger label and the class label — the two the pipeline reads.

    The trigger label is what `sentinel.github.events` dispatches on, and the class label is what
    selects the playbook and the ACU cap (`docs/05-devin-integration.md`). An issue carrying one
    without the other either never starts or starts against the wrong budget.
    """
    return (settings.autofix_label, f"{CLASS_LABEL_PREFIX}{candidate.issue_class}")


def issue_body(candidate: Candidate, triage: Triage, settings: TargetSettings) -> str:
    """The issue as an agent that has read nothing else receives it."""
    rows = [f"| Class | `{candidate.issue_class}` |"]
    if candidate.evidence_strength:
        rows.append(f"| Evidence | {_delink(candidate.evidence_strength)} |")
    if triage.provenance is not None:
        rows.append(f"| Evidence read on | {triage.provenance} |")

    parts = [
        "\n".join(["| | |", "|---|---|", *rows]),
        *(
            f"## {title}\n\n{_delink(candidate.sections[name])}"
            for name, title in BODY_SECTIONS
            if name in candidate.sections
        ),
    ]

    if candidate.caveats:
        parts.append(
            "## What could not be verified\n\n"
            "Stated so that it is checked rather than trusted. If one of these turns out to be "
            "wrong, say so in the pull request — that is worth more than a fix built on it.\n\n"
            + "\n".join(f"- {_delink(caveat)}" for caveat in candidate.caveats)
        )

    parts.append(
        "## Definition of done\n\n" + DEFINITION_OF_DONE.format(branch=settings.target_base_branch)
    )
    parts.append(
        f"---\n\nFiled by Sentinel from its triage of this fork ({candidate.id} of "
        f"`docs/remediation-candidates.md`). The evidence above was read on a checkout that has "
        "since moved on, so check each file and line against the tree as it is now rather than "
        "assuming they still line up."
    )
    return "\n\n".join(parts)


def _delink(text: str) -> str:
    """Replace links into Sentinel's own repository with the path they point at.

    The issue is read on `taxpon/superset`, where a relative link resolves to a file that is not
    there — and Sentinel's repository is not one the reader can open either. Naming the document
    keeps the citation honest; leaving the link would offer a reader something that 404s. Absolute
    links are left alone: an upstream issue or an advisory is exactly what should be followed.
    """
    return _RELATIVE_LINK.sub(
        lambda link: f"`docs/{link.group('path').removeprefix('./').split('#')[0]}`", text
    )


# --- The fork -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Label:
    """A label the issues carry, and therefore one the fork has to have."""

    name: str
    color: str
    description: str | None = None

    def creation(self) -> dict[str, str]:
        body = {"name": self.name, "color": self.color}
        if self.description is not None:
            body["description"] = self.description
        return body


def required_labels(candidates: Sequence[Candidate], settings: TargetSettings) -> tuple[Label, ...]:
    """Every label the issues about to be filed carry, in the order they are created.

    Only those — not the whole set `scripts/bootstrap_github.py` reconciles. That script is the
    fork's configuration and owns the label set; this one is filing eight issues and needs the
    labels those eight issues use to exist, so that a fork nobody has bootstrapped yet produces
    eight issues that dispatch rather than eight GitHub rejects the moment a label is unknown.
    """
    classes = dict.fromkeys(candidate.issue_class for candidate in candidates)
    return (
        Label(settings.autofix_label, TRIGGER_LABEL_COLOR, TRIGGER_LABEL_DESCRIPTION),
        *(
            Label(f"{CLASS_LABEL_PREFIX}{issue_class}", CLASS_LABEL_COLOR)
            for issue_class in classes
        ),
    )


class Fork:
    """The target repository, over the four calls this script makes.

    Deliberately not `sentinel.github.client.GitHubClient`: that client's `ROUTES` table is the
    statement of everything the *running pipeline* may do to the repository, and creating a label or
    opening an issue is neither of its business — the pipeline acts on issues other people filed.
    `sentinel.scanner.audit.TargetRepository` draws the same line for the same reason.
    """

    def __init__(
        self,
        http: httpx.Client,
        repo: str,
        *,
        secrets: frozenset[str],
        dry_run: bool = True,
    ) -> None:
        self._http = http
        self._secrets = secrets
        self.repo = repo
        self.dry_run = dry_run

    def __repr__(self) -> str:
        # Names the repository the run is about, where the default names an address.
        return f"Fork(repo={self.repo!r}, dry_run={self.dry_run!r})"

    @property
    def repo_path(self) -> str:
        return f"/repos/{self.repo}"

    def issue_titles(self) -> set[str]:
        """The title of every issue on the fork, **open and closed**.

        Closed ones count. An issue somebody filed and closed has been decided, and filing it again
        because the state filter did not reach it would overrule that decision silently.

        Pull requests are dropped. `GET /issues` returns them too, and one of Sentinel's own
        remediation pull requests takes its title from the issue it closes — which would make the
        issue look filed twice and, worse, make a re-run skip a candidate whose issue was never
        opened.
        """
        return {
            str(issue.get("title") or "")
            for issue in self._collection(f"{self.repo_path}/issues", {"state": "all"})
            if "pull_request" not in issue
        }

    def label_names(self) -> set[str]:
        """Every label on the fork, lower-cased — the way GitHub compares them.

        `Class:bug` and `class:bug` cannot coexist there, so a case-sensitive lookup would report a
        label as missing and then fail to create it.
        """
        return {
            str(label.get("name") or "").lower()
            for label in self._collection(f"{self.repo_path}/labels", {})
        }

    def create_label(self, label: Label) -> None:
        self._write("POST", f"{self.repo_path}/labels", label.creation())
        self.change(f"label {label.name}: created")

    def create_issue(self, *, title: str, body: str, labels: Sequence[str]) -> str | None:
        """File one issue and return where it landed. `None` on a dry run, which files nothing."""
        created = self._write(
            "POST",
            f"{self.repo_path}/issues",
            {"title": title, "body": body, "labels": list(labels)},
        )
        url = created.get("html_url")
        return str(url) if isinstance(url, str) else None

    def change(self, message: str) -> None:
        """Report one line of what happened, marked when it did not actually happen."""
        print(f"  {'[dry run] ' if self.dry_run else ''}{message}")

    def _write(self, method: str, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """One mutating call, or nothing at all on a dry run.

        Every write in this script goes through here, so "a dry run creates nothing" is one branch
        to read rather than a property spread over four call sites.
        """
        if self.dry_run:
            return {}
        response = self._request(method, path, json=body)
        decoded = self._decoded(response, method, path) if response.content else {}
        return decoded if isinstance(decoded, dict) else {}

    def _collection(self, path: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Every item of a paginated collection, following `Link: rel="next"`.

        Both collections outgrow a page in the ordinary case: the fork carries only GitHub's nine
        default labels today, but `scripts/bootstrap_github.py` adds eleven more and the issue list
        grows with every remediation. Stopping at the first page would make an existing label look
        missing — and an issue list read one page deep
        would make an issue that is already filed look absent, which is the duplicate this script
        exists to avoid.
        """
        items: list[dict[str, Any]] = []
        url: str | None = path
        query: dict[str, Any] | None = {**params, "per_page": PAGE_SIZE}
        while url is not None:
            response = self._request("GET", url, params=query)
            page = self._decoded(response, "GET", url)
            if not isinstance(page, list):
                raise FilingError(f"GET {url} did not answer with an array")
            items += [item for item in page if isinstance(item, dict)]
            following = response.links.get("next")
            # The next URL carries the cursor and the page size already.
            url, query = (following["url"], None) if following else (None, None)
        return items

    def _request(self, method: str, path: str, **options: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **options)
        except httpx.HTTPError as exc:
            # `from None` rather than `from exc`: the chained exception carries the request, and
            # the request carries the Authorization header.
            raise FilingError(self._redact(f"{method} {path} failed: {exc}")) from None
        if response.is_success:
            return response
        raise FilingError(
            f"{method} {path} failed with {response.status_code}: "
            f"{self._excerpt(response)}{_hint(response)}"
        )

    def _decoded(self, response: httpx.Response, method: str, path: str) -> Any:
        """The JSON body, or a `FilingError` naming the request that produced something else."""
        try:
            return response.json()
        except ValueError:
            raise FilingError(
                f"{method} {path} did not answer with JSON: {self._excerpt(response)}"
            ) from None

    def _excerpt(self, response: httpx.Response) -> str:
        text = self._redact(" ".join(response.text.split()))
        return text[:BODY_EXCERPT_CHARS] if text else "no response body"

    def _redact(self, text: str) -> str:
        """Take every configured credential back out of anything on its way to a terminal.

        GitHub echoes a rejected payload back in the `errors` of a `422`, and an error message is
        the one place in a script that prints where a token could otherwise surface.
        """
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


def _hint(response: httpx.Response) -> str:
    """What to do about the three answers that mean something other than "it failed"."""
    if response.status_code == 429 or (
        response.status_code == 403
        and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
        )
    ):
        return " — rate limited; wait and re-run."
    if response.status_code == 403:
        return " — check the token's permissions: filing issues needs Issues: write."
    if response.status_code == 410:
        return (
            " — the fork's issue tracker is switched off (B1). Run `make bootstrap-github` first; "
            "there is nowhere to file to until it is on."
        )
    return ""


# --- The run --------------------------------------------------------------------------------------


def sync_labels(fork: Fork, labels: Sequence[Label]) -> None:
    """Create the labels the issues carry that the fork does not have. Nothing is patched here.

    Reconciling a label a human has since re-coloured belongs to `scripts/bootstrap_github.py`,
    which owns the fork's configuration. This run only needs each label to exist.
    """
    existing = fork.label_names()
    missing = [label for label in labels if label.name.lower() not in existing]
    print(f"labels: {len(labels)} carried by these issues, {len(missing)} missing")
    for label in missing:
        fork.create_label(label)


def file_issues(
    fork: Fork,
    triage: Triage,
    settings: TargetSettings,
    *,
    show_bodies: bool = False,
) -> int:
    """File every candidate the fork does not already carry an issue for. Returns how many."""
    existing = fork.issue_titles()
    print(f"issues: {len(triage.candidates)} candidates, {len(existing)} already on the fork")

    filed = 0
    for candidate in triage.candidates:
        title = issue_title(candidate)
        if title in existing:
            print(f"  {candidate.id} skipped: an issue titled {title!r} already exists")
            continue
        labels = issue_labels(candidate, settings)
        body = issue_body(candidate, triage, settings)
        url = fork.create_issue(title=title, body=body, labels=labels)
        fork.change(
            f"{candidate.id} filed: {title}\n"
            f"      labels {', '.join(labels)} · {len(body)} characters"
            + (f"\n      {url}" if url else "")
        )
        if show_bodies:
            print(_quoted(body))
        filed += 1
    return filed


def _quoted(body: str) -> str:
    """An issue body indented under the line that announced it, so a transcript stays readable."""
    return "\n".join(f"      | {line}".rstrip() for line in body.splitlines())


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="File the remediation candidates of docs/remediation-candidates.md as issues "
        "on the target repository. Reports what it would file unless --apply is given."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually create the issues. Without it nothing is written.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be filed and write nothing. This is the default.",
    )
    parser.add_argument(
        "--document",
        type=Path,
        default=CANDIDATES,
        metavar="PATH",
        help=f"The candidates to file (default: {CANDIDATES}).",
    )
    parser.add_argument(
        "--show-bodies",
        action="store_true",
        help="Print each issue body in full, so it can be read before --apply is typed.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    dry_run = not args.apply

    try:
        settings = load_config(GitHubSettings)
    except ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return EXIT_MISCONFIGURED

    try:
        triage = parse(args.document.read_text(encoding="utf-8"))
    except (OSError, DocumentError) as exc:
        print(f"{args.document}: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE_DOCUMENT

    print(
        f"repository {settings.target_repo}"
        + (" (dry run — nothing is written; pass --apply to file)" if dry_run else "")
    )
    classes = ", ".join(sorted({str(c.issue_class) for c in triage.candidates}))
    print(f"{args.document}: {len(triage.candidates)} candidates across {classes}")

    try:
        with httpx.Client(
            base_url=GITHUB_API_BASE,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                # Distinct from the pipeline's `sentinel`, so the fork's audit log separates the
                # issues an operator filed from what the running system does.
                "User-Agent": "sentinel-remediation",
            },
        ) as http:
            fork = Fork(
                http,
                settings.target_repo,
                secrets=secret_values(settings),
                dry_run=dry_run,
            )
            sync_labels(fork, required_labels(triage.candidates, settings))
            filed = file_issues(fork, triage, settings, show_bodies=args.show_bodies)
    except FilingError as exc:
        print(f"filing failed: {exc}", file=sys.stderr)
        print("Nothing is undone; re-run to file the rest.", file=sys.stderr)
        return EXIT_STEP_FAILED

    if dry_run:
        print(
            f"\nnothing was written. {filed} issue(s) would be filed; re-run with --apply to "
            "file them."
        )
    else:
        print(f"\nfiled {filed} issue(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
