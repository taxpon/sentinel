"""The nightly dependency sweep: resolve the target's manifests against OSV, and file the findings
that are worth a remediation.

`docs/05-devin-integration.md#scheduled-sweep` closes the loop — the sweep produces the issues the
rest of the pipeline consumes. That makes three properties load-bearing, and each is decided here
rather than left to the caller.

**One advisory source, both ecosystems.** Everything is resolved against the OSV API: the pins in
`requirements/*.txt` as PyPI, and the resolved tree in `superset-frontend/package-lock.json` as npm.
`npm audit` is not run — see
`docs/adr/2026-08-08-both-ecosystems-are-resolved-against-osv.md`.

**A filed advisory is never filed twice.** Every issue this module writes carries a machine-readable
fingerprint, and the sweep reads the fingerprints back off the issues it has already filed — open
*and* closed — before filing anything. `docs/adr/2026-08-08-the-filed-issue-is-the-sweeps-memory.md`
records why the memory lives on GitHub rather than in Sentinel's database.

**Most findings are not filed.** A raw scan of this fork produces dozens of advisories and almost
all of them resolve to regenerating a lock file. `triage` rejects those, and
`docs/adr/2026-08-08-the-sweep-files-only-what-forces-a-decision.md` records the rule and what it
gets wrong. `docs/remediation-candidates.md` is the worked example the rule was calibrated against:
paramiko and the deck.gl family survive it, `flask`, `cryptography`, `dompurify`, `setuptools` and
`xlsx` do not.

The GitHub surface is `TargetRepository`, and it is deliberately not part of
`sentinel.github.client`: that client's `ROUTES` table is the complete list of endpoints the
*remediation* pipeline can reach, and reading a manifest and opening an issue are neither of its
business nor safe to grant it. The sweep holds its own two-and-a-half endpoints instead.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Final, Protocol, Self

import httpx

from sentinel.config import Settings, get_settings
from sentinel.devin.playbooks import IssueClass
from sentinel.observability.logging import get_logger

log = get_logger(__name__)

OSV_API_BASE: Final = "https://api.osv.dev"
GITHUB_API_BASE: Final = "https://api.github.com"
API_VERSION: Final = "2022-11-28"

REQUEST_TIMEOUT_SECONDS: Final = 30.0
PAGE_SIZE: Final = 100

QUERY_BATCH_SIZE: Final = 500
"""Packages per `POST /v1/querybatch`. OSV documents a ceiling of 1000; half of it keeps one
request well inside the timeout on a tree with ~3,800 resolved packages."""

PYPI: Final = "PyPI"
NPM: Final = "npm"

ECOSYSTEM_CLASS: Final[Mapping[str, IssueClass]] = {
    PYPI: IssueClass.SECURITY_DEP,
    NPM: IssueClass.FRONTEND_DEP,
}
"""Which class label a finding is filed under. The label is not decoration: it selects the playbook
and the ACU ceiling (`docs/05-devin-integration.md#playbooks-and-acu-caps`), and the two dependency
classes are described separately in `docs/01-overview.md#issue-classes` — a CVE in a Python
dependency and an npm advisory are different work with different budgets."""

_CLASS_LABELS: Final[tuple[str, ...]] = tuple(
    f"class:{issue_class}" for issue_class in ECOSYSTEM_CLASS.values()
)
"""The labels the sweep files under, and therefore the only ones its own issues can carry. Listing
by label rather than reading the whole issue list keeps the lookup bounded on a repository that
also carries the seven other classes and everything a human filed."""


REQUIREMENTS: Final[tuple[str, ...]] = (
    "requirements/base.txt",
    "requirements/development.txt",
)
"""The pip-compile outputs the sweep reads. Named rather than discovered: the manifests are fetched
one at a time over the contents API, so there is no directory to list, and a requirements file that
is added to the fork should be added here deliberately."""

PYPROJECT: Final = "pyproject.toml"
FRONTEND_PACKAGE_JSON: Final = "superset-frontend/package.json"
FRONTEND_LOCKFILE: Final = "superset-frontend/package-lock.json"

MAX_ISSUES_PER_SWEEP: Final = 3
"""How many issues one run may file.

The sweep runs nightly, so this is a rate, not a total: a real backlog drains at three a night
instead of arriving in one morning. Three is the number the rest of the system is already sized for
— `MAX_CONCURRENT_SESSIONS` defaults to 3, and three `dep-upgrade` sessions at their 10-ACU ceiling
is 30 of the 100-ACU daily budget (`docs/09-operations.md#configuration`), which leaves the
webhook-driven work the budget guard is actually there to protect. Findings over the limit are
reported as deferred, not suppressed: the next run reconsiders them, and by then the ones filed
tonight are closed."""

MARKER: Final = "sentinel-advisory"
_MARKER_LINE: Final = re.compile(
    rf"<!--\s*{MARKER}:\s*(?P<ecosystem>[^/\s]+)/"
    r"(?P<package>@[^/\s]+/[^/\s]+|[^/\s]+)/(?P<advisory>[^\s]+)\s*-->"
)
"""The fingerprint carried by every issue the sweep files, and the only thing it reads back.

The package alternative is not `[^/\\s]+`. An npm scope is part of the name and carries a slash of
its own, so `npm/@deck.gl/mesh-layers/GHSA-…` has four segments rather than three — and a pattern
that assumed three would read the package as `@deck.gl`, never match the finding it was written
for, and re-file the issue every night. The deck.gl family is precisely the population this sweep
is calibrated to find (`docs/remediation-candidates.md`, C4), so the scoped form is the case that
matters rather than an edge of it."""


# --- What the sweep refuses to file ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Suppression:
    """One advisory the sweep will never file, and the reason it will never file it.

    Distinct from the rules in `triage`, which are judgements about a *shape* of finding. These are
    judgements about a specific advisory against a specific package in this specific tree, made
    once by a human and recorded here so that the sweep does not re-litigate them nightly. Every
    entry names the evidence, because an unexplained suppression is indistinguishable from a bug.
    """

    ecosystem: str
    package: str
    advisory: str
    reason: str


SUPPRESSED: Final[tuple[Suppression, ...]] = (
    Suppression(
        NPM,
        "xlsx",
        "GHSA-4r6h-8v6p-xvw6",
        "No version constraint can satisfy this advisory. It carries `introduced: 0` with no fixed "
        "version because the npm package is abandoned; the patched builds exist only on the "
        "SheetJS CDN, and `superset-frontend/package.json` already pins the CDN tarball at 0.20.3, "
        "above both patched releases (docs/remediation-candidates.md).",
    ),
    Suppression(
        NPM,
        "xlsx",
        "GHSA-5pgg-2g8v-p4x9",
        "The sibling of GHSA-4r6h-8v6p-xvw6 on the same abandoned package, and unsatisfiable for "
        "the same reason (docs/remediation-candidates.md).",
    ),
)

BUILD_TOOLCHAIN: Final[frozenset[str]] = frozenset(
    {"pip", "setuptools", "wheel", "pip-tools", "virtualenv", "build", "twine"}
)
"""Packages that are how the environment is built, not part of what Superset ships.

`pip-compile` pins them because they were present when it ran, so OSV finds them and reports them
like any other dependency — five separate advisories on `pip` alone in the current tree, which is
enough to fill a whole sweep with work nobody would do. Nothing under `superset/` imports any of
them, so there is no call site to adapt, and the version that matters at runtime is the one the CI
image installs rather than the one in a requirements file. `setuptools` is the case
`docs/remediation-candidates.md` worked through by hand and rejected: it is held by an explicit
`setuptools<81` pin whose reason is recorded in `requirements/base.in` and
`docs/docs/contributing/pkg-resources-migration.md`, so filing it would contradict a recorded
decision to gain nothing. The rule generalises that rejection instead of restating it per
advisory."""


class Rejected(StrEnum):
    """Why a finding was not filed. Reported, never silent — see `SweepReport`."""

    SUPPRESSED = "suppressed"
    WITHDRAWN = "withdrawn"
    TOOLCHAIN = "build-toolchain"
    NO_REMEDIATION = "no-remediation-available"
    UNREACHABLE = "no-manifest-line"
    LOCK_REFRESH = "lock-refresh-only"
    ALREADY_FILED = "already-filed"
    COVERED = "answered-by-another-issue"
    OVER_LIMIT = "deferred-to-the-next-sweep"


# --- Versions --------------------------------------------------------------------------------

_NUMERIC: Final = re.compile(r"\d+(?:\.\d+)*")


def release(version: str) -> tuple[int, ...]:
    """The leading numeric components of a version: `3.5.1` and `1.0.0-beta.2` and `v2.0` alike.

    Pre-release and local segments are dropped. This module only ever compares a version against
    another version of the same package to answer "is the fix above what is installed" and "does
    the declared range admit it", and on that question `1.0.0-rc1` and `1.0.0` are the same answer.
    A full PEP 440 / semver implementation would need a dependency the project does not have.
    """
    found = _NUMERIC.search(version.strip().removeprefix("v").removeprefix("="))
    return tuple(int(part) for part in found.group().split(".")) if found else ()


def _pad(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)), right + (0,) * (width - len(right))


def _compare(left: str, right: str) -> int:
    first, second = _pad(release(left), release(right))
    return (first > second) - (first < second)


def caret_bound(version: str) -> tuple[int, ...]:
    """The prefix a caret range pins: semver's **leftmost non-zero** component.

    `^3.5.1` admits `3.9.0`, `^0.7.5` refuses `0.8.0`, and `^0.0.3` refuses even `0.0.4` — each
    leading zero shifts the boundary one place right, because a project that has not reached 1.0
    has promised nothing about the components above it. A version that is zero throughout has no
    non-zero component to find and is pinned whole.
    """
    parts = release(version)
    for index, part in enumerate(parts):
        if part:
            return parts[: index + 1]
    return parts


def significant(version: str) -> tuple[int, ...]:
    """The component a resolver will not cross on its own — the boundary `_lock_refresh` falls back
    to when no manifest declares a range.

    The major, or the minor below 1.0. Deliberately one tier coarser than `caret_bound`, and the
    two disagree only about `0.0.x`. This is not the caret rule and must not be used as one: it
    answers "would regenerating the lock have carried this quietly", and two zeros deep the answer
    is yes for the ecosystem this mostly meets. `python-multipart` sits at `0.0.29` with four
    advisories fixed in `0.0.30` and `0.0.31`; `pip-compile` picks those up without anybody being
    asked, and treating each as a forced decision would file four issues for one `pip-compile` run
    — the noise `docs/adr/2026-08-08-the-sweep-files-only-what-forces-a-decision.md` exists to
    remove. The cost is that an npm `0.0.x` patch, which a caret range really would refuse, is
    treated as a lock refresh; that errs toward filing less, which is the direction to err in.
    """
    parts = release(version)
    if not parts:
        return ()
    return parts[:2] if parts[0] == 0 else parts[:1]


# --- Declared constraints --------------------------------------------------------------------

_Clause = tuple[str, tuple[int, ...]]

_COMPARATOR: Final = re.compile(r"(>=|<=|==|!=|~=|>|<|=)?\s*([0-9][0-9A-Za-z.\-+*]*)")


def _clauses(text: str) -> list[_Clause] | None:
    """A whitespace- or comma-separated comparator list, or `None` if anything is unrecognised."""
    parsed: list[_Clause] = []
    for token in text.replace(",", " ").split():
        match = _COMPARATOR.fullmatch(token)
        if match is None:
            return None
        operator, version = match.group(1) or "=", match.group(2)
        parts = release(version)
        if not parts:
            return None
        if operator == "~=":
            # PEP 440's compatible release drops the last component and raises the one before it:
            # `~=3.4.1` is `>=3.4.1, <3.5`, and `~=3.4` is `>=3.4, <4`.
            if len(parts) < 2:
                return None
            parsed += [(">=", parts), ("<", (*parts[:-2], parts[-2] + 1))]
        else:
            parsed.append(("==" if operator == "=" else operator, parts))
    return parsed or None


def _npm_clauses(text: str) -> list[_Clause] | None:
    """One npm range, as comparators. `None` for anything this does not understand.

    Carets and tildes cover essentially every entry in Superset's manifests. A range that is a URL,
    a tarball, a `workspace:` or a `npm:` alias resolves to `None`, which `triage` reads as "the
    project has expressed no opinion" rather than as an answer — the CDN-pinned `xlsx` entry is
    exactly that shape.
    """
    if any(marker in text for marker in ("://", ":", "|")):
        return None
    body = text.strip()
    if not body or body in {"*", "x", "latest"}:
        return None
    if body[0] in "^~":
        parts = release(body[1:])
        if not parts:
            return None
        # A caret pins up to the leftmost non-zero component; a tilde always pins the minor when
        # one is written, so `~0.0.3` is `<0.1.0` where `^0.0.3` is `<0.0.4`.
        upper = caret_bound(body[1:]) if body[0] == "^" else parts[:2] or parts
        return [(">=", parts), ("<", (*upper[:-1], upper[-1] + 1))]
    return _clauses(body)


def _holds(clause: _Clause, version: tuple[int, ...]) -> bool:
    operator, bound = clause
    left, right = _pad(version, bound)
    match operator:
        case ">=":
            return left >= right
        case ">":
            return left > right
        case "<=":
            return left <= right
        case "<":
            return left < right
        case "!=":
            return left != right
        case _:
            return left == right


def admits(constraint: str, ecosystem: str, version: str) -> bool | None:
    """Whether the declared range already permits `version`. `None` when it cannot be read.

    `None` and `False` are different answers and `triage` treats them differently: `False` means
    the manifest has to change, `None` means the manifest says nothing useful and the decision
    falls to the version boundary instead.
    """
    parsed = _npm_clauses(constraint) if ecosystem == NPM else _clauses(constraint)
    if parsed is None:
        return None
    parts = release(version)
    return all(_holds(clause, parts) for clause in parsed) if parts else None


# --- The tree --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceLine:
    """A place in a manifest, as the issue body names it: `requirements/base.txt:283`.

    `line` is `None` for a manifest the sweep read as data rather than as text — a workspace
    `package.json` reached through the lockfile. The path still identifies the file to edit.
    """

    path: str
    line: int | None
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}" if self.line is not None else self.path


@dataclass(frozen=True, slots=True)
class Dependency:
    """One resolved package, and everywhere the repository would have to change to move it.

    The three locations answer different questions and a finding may have any subset of them:

    - `pinned_at` — the line stating the installed version. Every PyPI package has one, because
      `pip-compile` pins the whole closure; no npm package does, because the lockfile is generated
      and nobody edits a line of it.
    - `declared_at` / `constraint` — what the project asks for *this* package, if it asks at all.
      This is the range `triage` tests the fix against.
    - `entry_at` / `chain` — for a package nothing declares, the declared dependency it hangs off
      and the path down to it. `image-size` is reached this way, and the manifest line that governs
      it belongs to `@deck.gl/mesh-layers` (`docs/remediation-candidates.md`, C4).
    """

    ecosystem: str
    name: str
    version: str
    pinned_at: SourceLine | None = None
    declared_at: SourceLine | None = None
    constraint: str | None = None
    entry_at: SourceLine | None = None
    chain: tuple[str, ...] = ()

    @property
    def manifest_line(self) -> SourceLine | None:
        """The line the issue tells a reader to open. `None` means nothing in the tree names this
        package at all, which is the one thing that makes a finding unactionable."""
        return self.pinned_at or self.declared_at or self.entry_at


def normalise(name: str) -> str:
    """PEP 503 normalisation. `Flask_Caching` and `flask-caching` are one package to PyPI, and OSV
    answers on the normalised name."""
    return re.sub(r"[-_.]+", "-", name).lower()


_PIN: Final = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*(?P<version>[^\s;#\\]+)"
)


def parse_requirements(path: str, text: str) -> list[Dependency]:
    """The `==` pins of one pip-compile output, with the line each is written on.

    Only `==`. A requirements file that is an input rather than an output carries ranges, `-r`
    includes and options, and none of those state an installed version — a finding has to name the
    version that is actually resolved or the issue cannot say what is affected.
    """
    found: list[Dependency] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _PIN.match(line)
        if match is None:
            continue
        found.append(
            Dependency(
                ecosystem=PYPI,
                name=normalise(match.group("name")),
                version=match.group("version"),
                pinned_at=SourceLine(path, number, line.split("#")[0].strip()),
            )
        )
    return found


def _locate(text: str, *needles: str) -> tuple[int, str] | None:
    """The first line containing every needle, 1-based, with the line itself."""
    for number, line in enumerate(text.splitlines(), start=1):
        if all(needle in line for needle in needles):
            return number, line.strip()
    return None


_REQUIREMENT: Final = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?")


def parse_pyproject(text: str) -> dict[str, SourceLine]:
    """Name to the line declaring it, for every requirement in `[project]`.

    Optional-dependency groups are included: Superset declares the database drivers there, and a
    driver's constraint governs its upgrade exactly as a core one's does.
    """
    project: Any = tomllib.loads(text).get("project", {})
    requirements: list[str] = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements += list(extra)

    declared: dict[str, SourceLine] = {}
    for requirement in requirements:
        match = _REQUIREMENT.match(requirement.strip())
        if match is None:
            continue
        name = normalise(match.group("name"))
        found = _locate(text, requirement)
        if name not in declared and found is not None:
            number, line = found
            declared[name] = SourceLine(PYPROJECT, number, line)
    return declared


def _constraint_of(line: SourceLine, name: str) -> str | None:
    """The range out of a declaration line, which carries the name as well: `flask>=2.2.5, <4.0.0`
    and `"@deck.gl/core": "~9.2.5",` both reduce to what follows the name.

    `None` when the declaration states no range at all. Superset declares twenty-odd requirements
    as a bare name — `"pgsanity"`, `"parsedatetime"` — and those express no opinion about which
    version is acceptable, which is a different thing from expressing one this cannot read.

    The name is matched after PEP 503 normalisation, not by prefix. `name` has been normalised
    (`flask-caching`) while the line carries the file's own spelling (`Flask_Caching>=2.1.0, <3`),
    and a comparison that tolerated case but not separators would leave the name attached to the
    range. `admits` would then answer `None` for a range it can read perfectly well, and
    `_lock_refresh` would fall through to a proxy for a question the manifest already answers.
    """
    if line.path.endswith(".json"):
        parts = line.text.split('"')
        return (parts[3] if len(parts) > 3 else "") or None

    body = line.text.split("#")[0].strip().strip('",')
    declared = _REQUIREMENT.match(body)
    if declared is None or normalise(declared.group("name")) != normalise(name):
        return body or None
    return body[declared.end() :].lstrip() or None


def python_dependencies(manifests: Mapping[str, str]) -> list[Dependency]:
    """Every pinned PyPI package, carrying the constraint `pyproject.toml` declares for it."""
    declared = parse_pyproject(manifests[PYPROJECT]) if PYPROJECT in manifests else {}
    found: dict[str, Dependency] = {}
    for path in REQUIREMENTS:
        text = manifests.get(path)
        if text is None:
            continue
        for pin in parse_requirements(path, text):
            # A package pinned in both files is one installed package; the first file wins so that
            # the issue names the manifest a reader would look in first.
            if pin.name in found:
                continue
            declaration = declared.get(pin.name)
            found[pin.name] = Dependency(
                ecosystem=PYPI,
                name=pin.name,
                version=pin.version,
                pinned_at=pin.pinned_at,
                declared_at=declaration,
                constraint=(
                    _constraint_of(declaration, pin.name) if declaration is not None else None
                ),
            )
    return list(found.values())


_DEPENDENCY_FIELDS: Final = ("dependencies", "optionalDependencies")


def _package_name(key: str, entry: Mapping[str, Any]) -> str:
    marker = "node_modules/"
    return key.rsplit(marker, 1)[1] if marker in key else str(entry.get("name") or key)


def _declared_in_package_json(text: str) -> dict[str, SourceLine]:
    manifest: Any = json.loads(text)
    declared: dict[str, SourceLine] = {}
    for field in ("dependencies", "devDependencies"):
        for name, wanted in manifest.get(field, {}).items():
            found = _locate(text, f'"{name}"', f'"{wanted}"')
            if name not in declared and found is not None:
                number, line = found
                declared[name] = SourceLine(FRONTEND_PACKAGE_JSON, number, line)
    return declared


def npm_dependencies(manifests: Mapping[str, str]) -> list[Dependency]:
    """Every package in the frontend lockfile, attributed to the manifest line that governs it.

    A transitive package has no line of its own, so the sweep walks the lockfile's dependency graph
    outward from the declared dependencies and keeps the shortest path that reaches it. The graph
    is keyed on package *name*, so a package installed twice at different versions collapses to one
    node: that is enough to name the direct dependency a maintainer would have to move, which is
    what the issue needs, and it is not enough to reconstruct npm's resolution — nor does anything
    here try to.
    """
    lockfile = manifests.get(FRONTEND_LOCKFILE)
    if lockfile is None:
        return []
    packages: Mapping[str, Any] = json.loads(lockfile).get("packages", {})

    root_json = manifests.get(FRONTEND_PACKAGE_JSON)
    declared = _declared_in_package_json(root_json) if root_json is not None else {}

    installed: dict[str, Dependency] = {}
    edges: dict[str, set[str]] = {}
    for key, entry in packages.items():
        name = _package_name(key, entry)
        requires: set[str] = set()
        for field in _DEPENDENCY_FIELDS:
            requires |= set(entry.get(field, {}))
        edges.setdefault(name, set()).update(requires)
        if "node_modules/" not in key:
            # The workspace's own manifest, not something installed. Its declarations are entry
            # points into the graph; the plugin manifests reach the deck.gl family this way.
            manifest = f"superset-frontend/{key}/package.json" if key else FRONTEND_PACKAGE_JSON
            for field in ("dependencies", "devDependencies"):
                for wanted_name, wanted in entry.get(field, {}).items():
                    declared.setdefault(
                        wanted_name,
                        SourceLine(manifest, None, f'"{wanted_name}": "{wanted}"'),
                    )
            continue
        version = entry.get("version")
        if isinstance(version, str) and name not in installed:
            installed[name] = Dependency(ecosystem=NPM, name=name, version=version)

    chains = _reachable(edges, declared)
    return [_attributed(dependency, declared, chains) for dependency in installed.values()]


def _reachable(
    edges: Mapping[str, set[str]], declared: Mapping[str, SourceLine]
) -> dict[str, tuple[str, ...]]:
    """The shortest path from a declared dependency down to every package the graph reaches."""
    chains: dict[str, tuple[str, ...]] = {name: (name,) for name in declared}
    queue: deque[str] = deque(chains)
    while queue:
        name = queue.popleft()
        for required in sorted(edges.get(name, ())):
            if required not in chains:
                chains[required] = (*chains[name], required)
                queue.append(required)
    return chains


def _attributed(
    dependency: Dependency,
    declared: Mapping[str, SourceLine],
    chains: Mapping[str, tuple[str, ...]],
) -> Dependency:
    declaration = declared.get(dependency.name)
    if declaration is not None:
        return Dependency(
            ecosystem=NPM,
            name=dependency.name,
            version=dependency.version,
            declared_at=declaration,
            constraint=_constraint_of(declaration, dependency.name),
        )
    chain = chains.get(dependency.name, ())
    return Dependency(
        ecosystem=NPM,
        name=dependency.name,
        version=dependency.version,
        entry_at=declared.get(chain[0]) if chain else None,
        chain=chain,
    )


def dependencies(manifests: Mapping[str, str]) -> list[Dependency]:
    """Everything installed, from every manifest the sweep was able to read."""
    return python_dependencies(manifests) + npm_dependencies(manifests)


# --- Advisories ------------------------------------------------------------------------------

_SEVERITY_ORDER: Final[Mapping[str, int]] = {
    "CRITICAL": 4,
    "HIGH": 3,
    "MODERATE": 2,
    "MEDIUM": 2,
    "LOW": 1,
}


@dataclass(frozen=True, slots=True)
class Advisory:
    """One OSV record, reduced to what an issue body needs.

    `identifiers` is the record's id together with every alias, because the same advisory reaches
    OSV several times over — paramiko's SHA-1 flaw is `GHSA-r374-rxx8-8654`, `PYSEC-2026-2858` and
    `CVE-2026-44405`, and a sweep that treated those as three findings would file three issues for
    one defect. `id` is the GHSA when there is one: it is the identifier the ecosystem's tooling
    and GitHub's own UI use, so it is the one a maintainer searching the issue list will type.
    """

    id: str
    identifiers: frozenset[str]
    summary: str
    severity: str
    withdrawn: bool
    references: tuple[str, ...]
    affected: Mapping[tuple[str, str], tuple[str | None, str | None]]

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(sorted(self.identifiers - {self.id}))

    @property
    def rank(self) -> int:
        return _SEVERITY_ORDER.get(self.severity, 0)


def _preferred(identifiers: Iterable[str]) -> str:
    ordered = sorted(identifiers)
    for prefix in ("GHSA-", "CVE-"):
        for identifier in ordered:
            if identifier.startswith(prefix):
                return identifier
    return ordered[0]


def _bounds(events: Iterable[Mapping[str, Any]], installed: str) -> tuple[str | None, str | None]:
    """The lowest fix above the installed version, and the lowest `last_affected` at or above it.

    An advisory patched on several branches carries several `fixed` events, and the one that
    matters is the nearest release above what is installed — telling a maintainer on 2.x to jump to
    4.1.0 when 2.7.3 carries the fix is a worse issue than filing none. `last_affected` is OSV's
    other way of closing a range and is the only bound paramiko's advisory has, which is why it is
    carried rather than discarded (`docs/remediation-candidates.md`, C2).
    """
    fixed = sorted(
        (str(event["fixed"]) for event in events if "fixed" in event),
        key=release,
    )
    last = sorted(
        (str(event["last_affected"]) for event in events if "last_affected" in event),
        key=release,
    )
    above = next((version for version in fixed if _compare(version, installed) > 0), None)
    at_or_above = next((version for version in last if _compare(version, installed) >= 0), None)
    return above, at_or_above


def parse_advisory(record: Mapping[str, Any], installed: Mapping[tuple[str, str], str]) -> Advisory:
    """One `GET /v1/vulns/{id}` body, resolved against the versions actually installed."""
    identifiers = frozenset({str(record["id"]), *(str(a) for a in record.get("aliases", ()))})
    affected: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for entry in record.get("affected", ()):
        package = entry.get("package", {})
        # OSV qualifies distro ecosystems as `Debian:11`; the part before the colon is the one that
        # matches what was queried.
        ecosystem = str(package.get("ecosystem", "")).split(":")[0]
        name = str(package.get("name", ""))
        key = (ecosystem, normalise(name) if ecosystem == PYPI else name)
        version = installed.get(key)
        if version is None:
            continue
        events = [event for span in entry.get("ranges", ()) for event in span.get("events", ())]
        fixed, last = _bounds(events, version)
        previous = affected.get(key, (None, None))
        affected[key] = (fixed or previous[0], last or previous[1])

    specific: Mapping[str, Any] = record.get("database_specific") or {}
    return Advisory(
        id=_preferred(identifiers),
        identifiers=identifiers,
        summary=str(record.get("summary") or "").strip(),
        severity=str(specific.get("severity") or "UNKNOWN").upper(),
        withdrawn="withdrawn" in record,
        references=tuple(
            str(reference["url"])
            for reference in record.get("references", ())
            if "url" in reference
        ),
        affected=affected,
    )


# --- Findings --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """One advisory against one installed package: the unit the sweep triages and files."""

    dependency: Dependency
    advisory: Advisory
    fixed: str | None
    last_affected: str | None

    @property
    def issue_class(self) -> IssueClass:
        return ECOSYSTEM_CLASS[self.dependency.ecosystem]

    @property
    def fingerprint(self) -> str:
        return f"{self.dependency.ecosystem}/{self.dependency.name}/{self.advisory.id}"

    @property
    def keys(self) -> frozenset[tuple[str, str, str]]:
        """Every fingerprint that would identify this finding, one per identifier the advisory is
        published under — so an issue filed when OSV answered `PYSEC-…` is still recognised once
        the GHSA record appears and becomes the preferred id."""
        return frozenset(
            (self.dependency.ecosystem, self.dependency.name, identifier)
            for identifier in self.advisory.identifiers
        )

    @property
    def target(self) -> str | None:
        """The version the fix is at, or the bound below it when OSV states no fix."""
        return self.fixed or self.last_affected


def findings(deps: Sequence[Dependency], advisories: Iterable[Advisory]) -> list[Finding]:
    """Pair every advisory with the packages it names, dropping the ones it does not."""
    by_key = {(dependency.ecosystem, dependency.name): dependency for dependency in deps}
    found: list[Finding] = []
    for advisory in advisories:
        for key, (fixed, last) in advisory.affected.items():
            dependency = by_key.get(key)
            if dependency is not None:
                found.append(Finding(dependency, advisory, fixed, last))
    return found


# --- Triage ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Skipped:
    finding: Finding
    reason: Rejected
    detail: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """One issue's worth of work: a package, and every advisory on it the same upgrade answers.

    `image-size 0.7.5` carries two advisories in the current tree, both closed by the same move.
    Filing them separately would put two issues in front of a maintainer for one upgrade, and —
    worse — start two Devin sessions that would each open a pull request against the same manifest.
    The issue is filed against `finding` and carries the fingerprints of `also` as well, so the
    siblings are recognised as filed on the next run rather than arriving one a night.
    """

    finding: Finding
    also: tuple[Finding, ...] = ()

    @property
    def advisories(self) -> tuple[Finding, ...]:
        return (self.finding, *self.also)

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return tuple(entry.fingerprint for entry in self.advisories)


@dataclass(frozen=True, slots=True)
class Triage:
    """What one sweep decided, in full. Nothing is discarded silently."""

    file: tuple[Candidate, ...]
    skipped: tuple[Skipped, ...]


def _suppression(finding: Finding) -> Suppression | None:
    return next(
        (
            entry
            for entry in SUPPRESSED
            if entry.ecosystem == finding.dependency.ecosystem
            and entry.package == finding.dependency.name
            and entry.advisory in finding.advisory.identifiers
        ),
        None,
    )


def _lock_refresh(finding: Finding) -> str | None:
    """Whether this fix is a lock regeneration rather than a decision somebody has to make.

    Two readings of the same question, in order of authority:

    1. **The project's own constraint.** If `pyproject.toml` or `package.json` already admits the
       fixed version, no manifest changes and the whole remediation is `pip-compile` or
       `npm install`. Superset's Flask advisory is this exactly — the lock is stale inside a range
       that already permits the fix.
    2. **The version boundary**, when nothing declares the package or the range cannot be read. A
       fix that leaves `significant()` unchanged is carried by regenerating the lock; one that
       crosses it forces every dependent to be checked. `dompurify` 3.4.12 to 3.4.13 is the first;
       `image-size` 0.7.5 to above 2.0.2 is the second.

    Both are proxies for "does a human have to decide something", and
    `docs/adr/2026-08-08-the-sweep-files-only-what-forces-a-decision.md` records what they get
    wrong in each direction.
    """
    dependency, target = finding.dependency, finding.target
    if target is None:
        return None
    if dependency.constraint is not None:
        permitted = admits(dependency.constraint, dependency.ecosystem, target)
        if permitted is True:
            return (
                f"`{dependency.constraint}` at {dependency.declared_at} already admits {target}, "
                "so no manifest line changes and the fix is a lock refresh."
            )
        if permitted is False:
            return None
    if significant(dependency.version) == significant(target):
        return (
            f"{dependency.version} to {target} stays inside the same major, so regenerating the "
            "lock carries the fix and no dependent has to be checked."
        )
    return None


def triage(
    candidates: Sequence[Finding], *, already_filed: frozenset[tuple[str, str, str]]
) -> Triage:
    """Decide, for every finding, whether it is worth an issue.

    The gates run in this order because each is cheaper and more certain than the next, and because
    a finding rejected by an earlier one should be reported under that reason: an advisory on the
    suppression list is suppressed whether or not it also has no fix.
    """
    keep: list[Finding] = []
    skipped: list[Skipped] = []
    for finding in candidates:
        suppressed = _suppression(finding)
        if suppressed is not None:
            skipped.append(Skipped(finding, Rejected.SUPPRESSED, suppressed.reason))
        elif finding.advisory.withdrawn:
            skipped.append(
                Skipped(finding, Rejected.WITHDRAWN, "The advisory has been withdrawn by OSV.")
            )
        elif finding.dependency.name in BUILD_TOOLCHAIN:
            skipped.append(
                Skipped(
                    finding,
                    Rejected.TOOLCHAIN,
                    f"`{finding.dependency.name}` is part of the packaging toolchain, not of what "
                    "Superset ships: nothing imports it, so there is no call site to adapt and no "
                    "regression a test could assert.",
                )
            )
        elif finding.target is None:
            skipped.append(
                Skipped(
                    finding,
                    Rejected.NO_REMEDIATION,
                    "OSV states neither a fixed version nor a last affected version, so there is "
                    "no version constraint that satisfies this advisory.",
                )
            )
        elif finding.dependency.manifest_line is None:
            skipped.append(
                Skipped(
                    finding,
                    Rejected.UNREACHABLE,
                    "No manifest in the repository names this package or anything that reaches "
                    "it, so an issue could not say what to change.",
                )
            )
        elif finding.keys & already_filed:
            skipped.append(
                Skipped(
                    finding,
                    Rejected.ALREADY_FILED,
                    f"An issue carrying {finding.fingerprint} has already been filed.",
                )
            )
        elif (refresh := _lock_refresh(finding)) is not None:
            skipped.append(Skipped(finding, Rejected.LOCK_REFRESH, refresh))
        else:
            keep.append(finding)

    keep.sort(
        key=lambda finding: (-finding.advisory.rank, finding.dependency.name, finding.advisory.id)
    )
    grouped = _group(keep)
    filing, deferred = grouped[:MAX_ISSUES_PER_SWEEP], grouped[MAX_ISSUES_PER_SWEEP:]
    skipped += [
        Skipped(
            finding,
            Rejected.OVER_LIMIT,
            f"No more than {MAX_ISSUES_PER_SWEEP} issues are filed per run; this one is left for a "
            "later run rather than filed alongside them.",
        )
        for candidate in deferred
        for finding in candidate.advisories
    ]
    # A sibling folded into a candidate gets no issue of its own, so it is reported here too. Its
    # absence would make `SweepReport.by_reason` count fewer findings than the run examined, and
    # the one number a reader checks first is whether those two agree.
    skipped += [
        Skipped(
            finding,
            Rejected.COVERED,
            f"Answered by the issue filed for {candidate.finding.advisory.id} on the same package: "
            "one upgrade fixes both.",
        )
        for candidate in filing
        for finding in candidate.also
    ]
    return Triage(file=tuple(filing), skipped=tuple(skipped))


def _group(surviving: Sequence[Finding]) -> list[Candidate]:
    """One candidate per package, highest-severity advisory first — see `Candidate`."""
    ordered: dict[tuple[str, str], list[Finding]] = {}
    for finding in surviving:
        ordered.setdefault((finding.dependency.ecosystem, finding.dependency.name), []).append(
            finding
        )
    return [Candidate(first, tuple(rest)) for first, *rest in ordered.values()]


# --- The issue -------------------------------------------------------------------------------

_WHAT_FIXED_MEANS: Final[Mapping[str, str]] = {
    PYPI: (
        "`{name}` is raised to a release that carries the fix, the constraint the project declares "
        "for it is widened to admit that release, every requirements file pinning it is "
        "regenerated, and the call sites the upgrade breaks are adapted."
    ),
    NPM: (
        "`{name}` resolves to a version at or above the fix, which means moving the declared "
        "dependency that pulls it in — and, where that dependency belongs to a family that has to "
        "move in lockstep, the rest of the family with it — and updating the call sites the move "
        "breaks."
    ),
}

_REGRESSION_TEST: Final[Mapping[str, str]] = {
    PYPI: (
        "A dependency floor is asserted by the manifest, so the regression test has to cover what "
        "the upgrade *changes*: find the modules under `superset/` that import `{name}`, name the "
        "existing host under `tests/unit_tests/**` that already covers them, and extend it so the "
        "adapted call sites are exercised. State the host in the pull request."
    ),
    NPM: (
        "The scoped jest run is the signal: the packages that have to move are the ones whose "
        "suites must pass across the bump. Name the suites under the touched package and say in "
        "the pull request what they do and do not prove — a lockfile advisory in a parser that "
        "nothing in the test suite feeds is not exercised by them, and the honest claim is that "
        "the upgrade did not break the call sites."
    ),
}


def _severity(advisory: Advisory) -> str:
    return advisory.severity.title() if advisory.severity != "UNKNOWN" else "not stated by OSV"


def _row(label: str, value: str) -> str:
    return f"| {label} | {value} |"


def issue_title(candidate: Candidate) -> str:
    dependency = candidate.finding.dependency
    return (
        f"{candidate.finding.issue_class}: {dependency.name} {dependency.version} "
        f"is affected by {candidate.finding.advisory.id}"
    )


def issue_body(candidate: Candidate, *, repo: str) -> str:
    """The issue a Devin session is given. Everything a remediation needs and nothing it can infer.

    The bar is the one `docs/remediation-candidates.md` set by hand: the advisory, the manifest
    line, the affected and fixed versions, what "fixed" means, and what a regression test would
    assert. Three things it also carries are less obvious and matter more:

    - **What OSV did not say.** An advisory that states only a last-affected version is filed with
      that fact in the body, because a session told "upgrade to 5.0.0" will do so without checking,
      and the version was inferred rather than published (C2).
    - **Why this one was filed.** The rule that let it past `triage` is stated, together with the
      instruction to report `blocked` if investigation shows the rule was wrong here. That makes a
      false positive cost one short session rather than a merged pin bump.
    - **The fingerprint of every advisory the upgrade answers**, at the bottom. It is what the next
      run reads, and it is deliberately visible rather than hidden in a database: a maintainer
      asking why the sweep stopped filing something can find the answer on the issue.
    """
    finding = candidate.finding
    dependency = finding.dependency
    advisory = finding.advisory

    rows = [
        _row("Advisory", " · ".join([f"**{advisory.id}**", *advisory.aliases])),
        _row("Severity", _severity(advisory)),
        _row("Ecosystem", dependency.ecosystem),
        _row("Installed", f"`{dependency.version}`"),
        _row("Fixed in", f"`{finding.fixed}`" if finding.fixed else "_not stated by OSV_"),
    ]
    if dependency.pinned_at is not None:
        rows.append(_row("Pinned at", f"`{dependency.pinned_at}` — `{dependency.pinned_at.text}`"))
    if dependency.declared_at is not None:
        rows.append(
            _row("Declared at", f"`{dependency.declared_at}` — `{dependency.declared_at.text}`")
        )
    if dependency.entry_at is not None:
        rows.append(
            _row(
                "Reached from",
                f"`{dependency.entry_at}` — `{dependency.entry_at.text}`, via "
                + " → ".join(f"`{name}`" for name in dependency.chain),
            )
        )

    sections = [
        f"`{dependency.name} {dependency.version}` in `{repo}` is affected by **{advisory.id}**.",
    ]
    if advisory.summary:
        sections.append(f"> {advisory.summary}")
    sections += [
        "\n".join(["| | |", "|---|---|", *rows]),
        "## What fixed means\n\n"
        + _WHAT_FIXED_MEANS[dependency.ecosystem].format(name=dependency.name),
    ]

    if finding.fixed is None:
        sections.append(
            "**The fixed version is not published.** OSV closes this range with "
            f"`last_affected: {finding.last_affected}` and no `fixed` event, so the release "
            "carrying the fix is above that and nothing here says which one. Establish it — from "
            "the project's own history, not from this issue — and say in the pull request how you "
            "established it."
        )

    if candidate.also:
        sections.append(
            f"## Also on `{dependency.name}`\n\n"
            + "\n".join(
                ["| Advisory | Severity | Fixed in |", "|---|---|---|"]
                + [
                    f"| {entry.advisory.id} | {_severity(entry.advisory)} | "
                    + (f"`{entry.fixed}`" if entry.fixed else "_not stated by OSV_")
                    + " |"
                    for entry in candidate.also
                ]
            )
            + "\n\nOne upgrade answers all of them, so they are tracked here rather than filed "
            "separately. Take the highest fixed version as the floor."
        )

    sections += [
        "## What a regression test must assert\n\n"
        + _REGRESSION_TEST[dependency.ecosystem].format(name=dependency.name),
        "## Why this was filed\n\n" + _why_filed(finding),
    ]

    if advisory.references:
        sections.append(
            "## References\n\n" + "\n".join(f"- {url}" for url in advisory.references[:5])
        )

    sections += [
        "---\n\nResolved against the [OSV](https://osv.dev) API by Sentinel's scheduled sweep "
        "(`docs/05-devin-integration.md#scheduled-sweep`). Reproduce with:\n\n"
        "```\n"
        "curl -s -X POST https://api.osv.dev/v1/querybatch -d '"
        + json.dumps(
            {
                "queries": [
                    {
                        "package": {
                            "name": dependency.name,
                            "ecosystem": dependency.ecosystem,
                        },
                        "version": dependency.version,
                    }
                ]
            }
        )
        + "'\n```",
        "\n".join(f"<!-- {MARKER}: {fingerprint} -->" for fingerprint in candidate.fingerprints),
    ]
    return "\n\n".join(sections)


def _why_filed(finding: Finding) -> str:
    """Which half of the gate let this through — read the same way `_lock_refresh` decided it.

    Not simply "is a constraint declared": a declared range this module cannot parse — a CDN
    tarball, a `workspace:` protocol — decides nothing, and the finding reached here on the version
    boundary instead. Claiming the constraint refused the fix would put a sentence in the issue
    that the manifest contradicts.
    """
    dependency = finding.dependency
    target = finding.target
    refused = (
        dependency.constraint is not None
        and target is not None
        and admits(dependency.constraint, dependency.ecosystem, target) is False
    )
    if refused:
        rule = (
            f"The fix is outside `{dependency.constraint}`, the constraint the project declares at "
            f"`{dependency.declared_at}`, so the manifest has to change and somebody has to decide "
            "what that constraint becomes."
        )
    else:
        rule = (
            f"No range the repository declares for `{dependency.name}` settles this, and the fix "
            f"moves it from {dependency.version} to above {target} — across the boundary that "
            "resolvers refuse to cross on their own, so every dependent has to be checked."
        )
    return (
        f"{rule} The sweep files only findings that force a decision of that kind; an advisory "
        "whose fix is already inside the declared range, or inside the same major, is a lock "
        "refresh and is not filed "
        "(`docs/adr/2026-08-08-the-sweep-files-only-what-forces-a-decision.md`).\n\n"
        "**If investigation shows that is wrong here** — the upgrade turns out to be a pin bump "
        "with no call site to adapt and no constraint to renegotiate — report `outcome: blocked` "
        "with that as the `blocked_reason` rather than opening a pull request for it. That is a "
        "useful answer, and it is how this rule gets corrected."
    )


def issue_labels(candidate: Candidate, settings: Settings) -> tuple[str, ...]:
    """`devin:autofix` and the class label.

    The trigger label is applied deliberately: `docs/01-overview.md` has the sweep's issues
    re-entering the pipeline at the labelled-issue step, which is what closes the loop. What stops
    that from being a burst is `MAX_ISSUES_PER_SWEEP` here and the concurrency cap and ACU budget
    in `sentinel.policy`, not the absence of a label.

    The class label is the ecosystem's: `security-dep` carries the `dep-upgrade` playbook for a
    Python dependency and `frontend-dep` the same playbook for an npm one, and they are separate
    classes in `docs/01-overview.md` because they are separate work.
    """
    return (settings.autofix_label, f"class:{candidate.finding.issue_class}")


# --- GitHub ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FiledIssue:
    number: int
    html_url: str
    title: str
    body: str


class IssueTracker(Protocol):
    """What the sweep needs of an issue tracker: read back what it filed, and file one more."""

    async def read(self, path: str) -> str | None: ...

    async def list_issues(self, label: str) -> Sequence[FiledIssue]: ...

    async def create_issue(self, *, title: str, body: str, labels: Sequence[str]) -> FiledIssue: ...


class ScannerError(RuntimeError):
    """A request the sweep could not complete. The schedule is the retry — see `sweep`."""


class TargetRepository:
    """The target repository, over the three endpoints the sweep needs.

    Deliberately separate from `sentinel.github.client.GitHubClient`, whose `ROUTES` table is
    documented as the complete list of endpoints the remediation pipeline can reach and is asserted
    against in `tests/test_github_client.py`. Reading a manifest and opening an issue are not that
    client's business: the pipeline acts on issues other people filed, and widening its route table
    to give the sweep two endpoints would widen it for every caller.

    There is no retry policy here, unlike that client. The sweep is a nightly job whose whole
    design is that running it twice files nothing twice, so a failed run is retried by the schedule
    — tomorrow, from the top, against a tree that may have changed — rather than in place.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        settings = get_settings() if settings is None else settings
        self._repo = settings.target_repo
        self._ref = settings.target_base_branch
        self._http = httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "sentinel",
            },
        )

    def __repr__(self) -> str:
        return f"TargetRepository(repo={self._repo!r})"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def read(self, path: str) -> str | None:
        """One file from the target's default branch, or `None` if it is not there.

        A missing manifest is an answer, not a failure: the fork may not carry
        `requirements/development.txt`, and a sweep that refused to run over the manifests it does
        have would report nothing at all.
        """
        response = await self._http.get(
            f"/repos/{self._repo}/contents/{path}",
            params={"ref": self._ref},
            headers={"Accept": "application/vnd.github.raw"},
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            log.info("scanner.manifest.absent", path=path)
            return None
        _raise_for(response, f"GET contents/{path}")
        return response.text

    async def list_issues(self, label: str) -> Sequence[FiledIssue]:
        """Every issue carrying `label`, **open and closed**, following pagination.

        Closed ones are the point. An advisory a maintainer looked at and closed has been decided;
        re-filing it next night would overrule that decision on a schedule, which is the behaviour
        that gets a bot switched off.
        """
        issues: list[FiledIssue] = []
        url: str | None = f"/repos/{self._repo}/issues"
        params: dict[str, Any] | None = {
            "state": "all",
            "labels": label,
            "per_page": PAGE_SIZE,
        }
        while url is not None:
            response = await self._http.get(url, params=params)
            _raise_for(response, "GET issues")
            issues += [
                FiledIssue(
                    number=item["number"],
                    html_url=item["html_url"],
                    title=item["title"],
                    body=item.get("body") or "",
                )
                # The issues endpoint returns pull requests as well, and one of Sentinel's own
                # remediation pull requests carries the class label it was filed under.
                for item in response.json()
                if "pull_request" not in item
            ]
            following = response.links.get("next")
            url, params = (following["url"], None) if following else (None, None)
        return issues

    async def create_issue(self, *, title: str, body: str, labels: Sequence[str]) -> FiledIssue:
        response = await self._http.post(
            f"/repos/{self._repo}/issues",
            json={"title": title, "body": body, "labels": list(labels)},
        )
        _raise_for(response, "POST issues")
        created = response.json()
        return FiledIssue(
            number=created["number"],
            html_url=created["html_url"],
            title=created["title"],
            body=created.get("body") or "",
        )


def _raise_for(response: httpx.Response, what: str) -> None:
    if not response.is_success:
        raise ScannerError(f"{what} failed with {response.status_code}")


class OsvClient:
    """The OSV API, over the two endpoints that answer "what is wrong with this tree".

    `POST /v1/querybatch` takes the whole resolved tree in batches and answers with advisory ids
    alone; `GET /v1/vulns/{id}` then gives the record. Both are unauthenticated, which is the
    property that makes this reproducible by anybody reading the issue
    (`docs/adr/2026-08-08-both-ecosystems-are-resolved-against-osv.md`).
    """

    def __init__(self, base_url: str = OSV_API_BASE) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": "sentinel"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def advisories(self, deps: Sequence[Dependency]) -> list[Advisory]:
        """Every advisory affecting these packages, resolved against the versions installed.

        Each record is parsed against **only the packages OSV said it affects at the version
        installed**, not against the whole tree. An OSV record's `affected` array covers every
        package and every version range the advisory has ever applied to, so a record naming
        `@babel/core` from 7.22.0 will list it whatever version is installed — and a sweep that
        matched the record's array against the tree would report a package sitting safely below the
        `introduced` event as vulnerable. `querybatch` has already answered that question per
        package and per version; keeping its answer is both correct and cheaper than re-deciding it
        from range arithmetic here.
        """
        installed = {
            (dependency.ecosystem, dependency.name): dependency.version for dependency in deps
        }
        parsed: list[Advisory] = []
        for identifier, packages in (await self._matches(deps)).items():
            record = await self._vuln(identifier)
            affected = {key: installed[key] for key in packages if key in installed}
            parsed.append(parse_advisory(record, affected))
        return _merge(parsed)

    async def _matches(self, deps: Sequence[Dependency]) -> dict[str, set[tuple[str, str]]]:
        """Advisory id to the packages `querybatch` reported it against.

        `results` is index-aligned with `queries`, which is the only place the pairing exists —
        the record fetched afterwards cannot say which of the packages it names was the one asked
        about.
        """
        found: dict[str, set[tuple[str, str]]] = {}
        for batch in _batched(deps, QUERY_BATCH_SIZE):
            response = await self._http.post(
                "/v1/querybatch",
                json={
                    "queries": [
                        {
                            "package": {
                                "name": dependency.name,
                                "ecosystem": dependency.ecosystem,
                            },
                            "version": dependency.version,
                        }
                        for dependency in batch
                    ]
                },
            )
            _raise_for(response, "POST /v1/querybatch")
            results = response.json().get("results", ())
            if len(results) != len(batch):
                # Nothing can be paired from a response of the wrong length, and pairing what does
                # line up would drop the tail — which is a silently incomplete sweep, the one
                # failure mode worse than a sweep that did not run.
                raise ScannerError(
                    f"POST /v1/querybatch answered {len(results)} results for {len(batch)} queries"
                )
            for dependency, result in zip(batch, results, strict=True):
                for vuln in result.get("vulns", ()):
                    found.setdefault(str(vuln["id"]), set()).add(
                        (dependency.ecosystem, dependency.name)
                    )
        return found

    async def _vuln(self, identifier: str) -> Mapping[str, Any]:
        response = await self._http.get(f"/v1/vulns/{identifier}")
        _raise_for(response, f"GET /v1/vulns/{identifier}")
        record: Mapping[str, Any] = response.json()
        return record


def _batched(items: Sequence[Dependency], size: int) -> Iterator[Sequence[Dependency]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _merge(advisories: Iterable[Advisory]) -> list[Advisory]:
    """Collapse the records that are aliases of each other into one advisory apiece.

    OSV answers for paramiko with both `GHSA-r374-rxx8-8654` and `PYSEC-2026-2858`, which are the
    same defect published twice. Grouping on the preferred identifier — which both records resolve
    to, because each lists the other — is what stops the sweep filing two issues for one advisory.
    Where the records disagree about the bounds, the one that names a fixed version wins: an
    advisory database that has since published the fix is better evidence than one that has not.
    """
    merged: dict[str, Advisory] = {}
    for advisory in advisories:
        existing = merged.get(advisory.id)
        if existing is None:
            merged[advisory.id] = advisory
            continue
        affected = dict(existing.affected)
        for key, (fixed, last) in advisory.affected.items():
            known = affected.get(key, (None, None))
            affected[key] = (known[0] or fixed, known[1] or last)
        merged[advisory.id] = Advisory(
            id=existing.id,
            identifiers=existing.identifiers | advisory.identifiers,
            summary=existing.summary or advisory.summary,
            severity=existing.severity if existing.rank >= advisory.rank else advisory.severity,
            withdrawn=existing.withdrawn and advisory.withdrawn,
            references=existing.references or advisory.references,
            affected=affected,
        )
    return list(merged.values())


# --- The sweep -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What one run did, including everything it decided not to do.

    `skipped` is not diagnostic noise. The sweep's value depends on it rejecting far more than it
    files, so the rejections are the part worth reading — a run that suddenly files eight issues,
    or stops filing any, shows up here before it shows up on the repository.
    """

    filed: tuple[FiledIssue, ...]
    skipped: tuple[Skipped, ...]
    scanned: int

    @property
    def by_reason(self) -> Mapping[Rejected, int]:
        counted: dict[Rejected, int] = {}
        for entry in self.skipped:
            counted[entry.reason] = counted.get(entry.reason, 0) + 1
        return counted


def filed_fingerprints(issues: Iterable[FiledIssue]) -> frozenset[tuple[str, str, str]]:
    """The advisories these issues already track, read off the markers in their bodies."""
    return frozenset(
        (match.group("ecosystem"), match.group("package"), match.group("advisory"))
        for issue in issues
        for match in _MARKER_LINE.finditer(issue.body)
    )


async def read_manifests(tracker: IssueTracker) -> dict[str, str]:
    manifests: dict[str, str] = {}
    for path in (PYPROJECT, *REQUIREMENTS, FRONTEND_PACKAGE_JSON, FRONTEND_LOCKFILE):
        text = await tracker.read(path)
        if text is not None:
            manifests[path] = text
    return manifests


async def sweep(
    *,
    tracker: IssueTracker,
    osv: OsvClient,
    settings: Settings | None = None,
) -> SweepReport:
    """One run: read the tree, resolve it against OSV, and file what survives triage.

    Idempotent by construction. The fingerprints are read from the issues already filed before
    anything is decided, so a second run on an unchanged tree files nothing and reports every
    finding as `already-filed`. That property is what makes a failed run safe to leave to the
    schedule rather than retry in place.
    """
    settings = get_settings() if settings is None else settings
    manifests = await read_manifests(tracker)
    resolved = dependencies(manifests)
    advisories = await osv.advisories(resolved)

    already = filed_fingerprints(
        [issue for label in _CLASS_LABELS for issue in await tracker.list_issues(label)]
    )
    decided = triage(findings(resolved, advisories), already_filed=already)

    log.info(
        "scanner.sweep.triaged",
        scanned=len(resolved),
        advisories=len(advisories),
        filing=len(decided.file),
        skipped=len(decided.skipped),
    )
    for entry in decided.skipped:
        log.info(
            "scanner.finding.skipped",
            package=entry.finding.dependency.name,
            advisory=entry.finding.advisory.id,
            reason=str(entry.reason),
            detail=entry.detail,
        )

    filed: list[FiledIssue] = []
    for candidate in decided.file:
        issue = await tracker.create_issue(
            title=issue_title(candidate),
            body=issue_body(candidate, repo=settings.target_repo),
            labels=issue_labels(candidate, settings),
        )
        log.info(
            "scanner.issue.filed",
            number=issue.number,
            url=issue.html_url,
            package=candidate.finding.dependency.name,
            advisories=list(candidate.fingerprints),
            issue_class=str(candidate.finding.issue_class),
        )
        filed.append(issue)

    return SweepReport(filed=tuple(filed), skipped=decided.skipped, scanned=len(resolved))
