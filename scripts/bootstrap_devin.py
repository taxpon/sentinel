#!/usr/bin/env python3
"""One-time setup of the Devin organisation, as `docs/09-operations.md#bootstrap` specifies it.

    make bootstrap-devin

It reads the Devin variables and nothing else — no `GITHUB_TOKEN`, no `DATABASE_URL` — because
nothing here talks to GitHub or stores anything. See `Configuration` below.

Four steps, in this order:

1. **token** — list the organisation's sessions. Nothing is created against a token Devin has not
   already accepted, so this runs first even though the spec lists it last: a `401` discovered
   after three notes exist is three notes to clean up by hand.
2. **tags** — `PUT /v3/organizations/{org}/tags` with the vocabulary of `devin/playbooks.py`
   ([B7](../docs/blockers.md)).
3. **knowledge** — the four notes of `docs/05-devin-integration.md#knowledge-notes`, whose ids are
   written into `.env` as `DEVIN_KNOWLEDGE_IDS`.
4. **schedule** — the nightly vulnerability sweep, whose id is written into `.env` as
   `DEVIN_SCHEDULE_ID`.

And a way to see all four before any of them happens:

    uv run scripts/bootstrap_devin.py --dry-run

which reports what the run would create and writes nothing — no request that changes anything, and
no line in `.env`. Step 1 still runs, and so do the capability probes: they are `GET`s, and a
preview that could not tell you the token is rejected would be worth much less than one that can.
What it says about each of the other three is what would *change* — the tag `PUT` sends the whole
vocabulary and replaces whatever is registered, and the notes and the sweep are created or skipped
according to what `.env` records, which is the thing that silently duplicates when `.env` has been
replaced from `.env.example`.

And one thing this file does that is not part of that run:

    make devin-playbooks

which lists the organisation's playbooks as title and id and prints the `DEVIN_PLAYBOOK_IDS` to
paste into `.env`. It is **read-only** — it creates, updates and deletes nothing, and does not touch
`.env` — because it exists precisely where the write path does not: the four playbooks are made by
hand in the Devin UI ([B6](../docs/blockers.md)), and the id a session must be created with is not
something the UI puts in front of whoever made them. It reads one variable fewer than the run above,
`DEVIN_PLAYBOOK_IDS` being the map it exists to fill in.

Then a **capability probe**, which is the part that earns the run. `docs/blockers.md` records B5
(enterprise session metrics) and B6 (enterprise playbook CRUD) as unverified because no credentials
existed to verify them with, and the whole `Degradable` design in `devin/schemas.py` exists in case
they are refused. The probe prints, in one table, which optional endpoints this deployment can
actually reach and what each unreachable one falls back to — so the degradation path is known
before the demo rather than during it.

A **refusal** is reported and never fails the run: it is an answer, and the fallback is the right
response to it. A **fault** — a `401`, a `500`, a dead connection — is not an answer, claims no
fallback, and leaves the run non-zero once the table has been printed. That line is `client.py`'s,
not this script's: it converts `403` and `404` into `Unavailable` and raises everything else, so
anything that reaches the probe's `except` is by construction something a fallback would paper over
(`docs/adr/2026-08-08-a-refusal-is-reported-a-fault-is-not.md`).

**Every step is idempotent.** This is run more than once — after a token is rotated, after a note
is edited, after a failure part-way through — and a second run must not leave a second schedule or
a fifth knowledge note behind. Step 2 is a `PUT` and idempotent by nature; steps 3 and 4 are
`POST`s and are made idempotent by what `.env` already records
(`docs/adr/2026-08-08-env-is-the-bootstrap-scripts-record.md`).

`.env` holds real credentials and is git-ignored. It is never read aloud, never truncated and never
reordered: a rewrite replaces the one assignment line it owns, keeps every other byte, and lands
through `os.replace`, so an interrupted write leaves the previous file intact rather than half of a
new one.

Unlike its neighbours in `scripts/`, this file carries no PEP 723 header. It imports `sentinel`,
which `uv run --script` would resolve in an isolated environment that does not contain the project;
without the header, `uv run scripts/bootstrap_devin.py` runs in the project environment, which is
how the Makefile invokes it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, TextIO

from pydantic import Field

from sentinel.config import (
    ConfigurationError,
    DevinSettings,
    PlaybookIds,
    ReportingSettings,
    TargetSettings,
    load_config,
)
from sentinel.devin.client import (
    DevinAPIError,
    DevinClient,
    DevinError,
    DevinTransportError,
)
from sentinel.devin.playbooks import NAMESPACE_TAG, PLAYBOOKS, TAG_PREFIXES, IssueClass
from sentinel.devin.schemas import Capability, PlaybookPage, Unavailability
from sentinel.observability.logging import configure_logging


class Configuration(DevinSettings, ReportingSettings):
    """What this script reads: the Devin organisation, the repository the nightly sweep is told to
    watch, and the level its own diagnostics are logged at. Not `GITHUB_TOKEN` and not a database —
    nothing here talks to GitHub or stores anything
    (`docs/adr/2026-08-10-a-script-loads-the-configuration-group-it-reads.md`)."""


class PlaybookLookup(Configuration):
    """What `--list-playbooks` reads: the same variables, minus the one it exists to find.

    `DEVIN_PLAYBOOK_IDS` is required of `api`, `worker` and `poller` and of the four steps above,
    because a session cannot be created without it. It cannot be required *here*: this option is
    what an operator runs when they do not have the ids yet, and demanding them first would make it
    unusable at the only moment it is wanted. So `DEVIN_API_TOKEN` and `DEVIN_ORG_ID` are the whole
    of what a lookup needs.
    """

    # A factory rather than a plain default: pydantic deep-copies a default, and a `mappingproxy`
    # cannot be pickled. The empty map is still immutable, which is what every other holder of this
    # field is.
    devin_playbook_ids: PlaybookIds = Field(default_factory=lambda: MappingProxyType({}))


# --- What is registered ---------------------------------------------------------------------------

VOCABULARY: tuple[str, ...] = (NAMESPACE_TAG, *(f"{prefix}:" for prefix in TAG_PREFIXES))
"""The organisation's allowed tag vocabulary, built from the two exports `devin/playbooks.py` calls
the single source of it. A tag Sentinel sends is either the namespace tag or one of these prefixes
with a value behind it — an issue number, a delivery id — so what is registered is the prefixes,
not an enumeration of every tag that will ever be sent.

*Unverified* (B8): that a registered *prefix* admits any value behind it is how
`devin/client.registered_tag` reads the vocabulary, and no credentials exist to confirm it against
Devin's own validation."""


@dataclass(frozen=True, slots=True)
class Note:
    """One knowledge note: what it is called, when Devin should reach for it, what it says."""

    name: str
    trigger: str
    body: str


NOTES: tuple[Note, ...] = (
    Note(
        name="Superset: running the tests",
        trigger="When running, choosing or adding tests in the Superset repository.",
        body="""\
Python tests live under `tests/`. Only `tests/unit_tests/**` runs in the CI that gates your pull
request; `tests/integration_tests/**` does not run at all, so a regression test placed there
produces no evidence that the fix works.

Mirror the module you changed. `superset/<pkg>/<mod>.py` is tested by
`tests/unit_tests/<pkg>/<mod>_test.py`, `test_<mod>.py` or `<mod>_tests.py` — all three spellings
are in the tree and `pytest.ini` declares `python_files = *_test.py test_*.py *_tests.py`. Add to
the file that already exists rather than introducing a fourth name.

Frontend tests are jest, run from `superset-frontend/` as `npm run test -- <path>`. Node is pinned
by `superset-frontend/.nvmrc`; the npm workspaces are `packages/*`, `plugins/*` and `src/setup/*`.

Slow, and worth not running in full: `tests/integration_tests/` (database-backed), the Cypress
suites under `superset-frontend/cypress-base/`, and the unsharded jest run.

Run the narrowest suite that covers the change, and state the exact command in the pull request
along with what it printed before and after the fix.""",
    ),
    Note(
        name="Superset: pre-commit and the lint gate",
        trigger="Before committing or pushing any change to the Superset repository.",
        body="""\
`.pre-commit-config.yaml` at the repository root is the lint gate. CI runs
`pre-commit run --files <changed files>` over exactly the files your diff touches; run the same
command locally before pushing.

Two hooks are skipped in CI and need not hold you up: `type-checking-frontend` (whole-project
`tsc`) and `eslint-docs`.

Several hooks rewrite files instead of failing — `ruff --fix` and the formatters among them — and
the job fails if the working tree is dirty afterwards. Run the hooks, then commit whatever they
changed: a green local run with uncommitted rewrites is a red CI run.

Do not silence a hook to get past it. No new `# noqa`, no new `# type: ignore`, no path added to a
tool's exclude list. If a rule is genuinely wrong for this change, say so in the pull request and
leave the rule alone.""",
    ),
    Note(
        name="Superset: pull request conventions",
        trigger="When opening or updating a pull request on the Superset repository.",
        body="""\
Title: a Conventional Commit, scoped to the area of the tree you touched — `fix(sqllab): ...`,
`perf(tags): ...`, `chore(deps): ...`.

The description states, in this order:

- the root cause — why the defect exists, not what you changed;
- what changed, one line per file;
- how it was verified: the exact test command, and its output before and after;
- anything deliberately left out, and why.

One pull request does one thing. Do not fold an unrelated cleanup, a formatting pass or a
dependency bump into a fix: a reviewer approves the whole diff or none of it.

Every pull request is read and merged by a human. Write the description for that reader.""",
    ),
    Note(
        name="Superset: directories that must not be touched",
        trigger="Before editing any file outside application source and its tests.",
        body="""\
Do not edit these, and do not let a tool edit them on your behalf:

- `superset/translations/**` — message catalogues, regenerated by the i18n tooling;
- `superset/static/assets/**` — build output;
- `superset-frontend/package-lock.json` and `requirements/*.txt` — lockfiles. Unless the task *is*
  a dependency upgrade, in which case regenerate them with the project's own tooling instead of
  editing them by hand;
- anything inside a `node_modules/`, `dist/` or `build/` directory;
- vendored third-party sources.

If the fix appears to require a change to one of these, that is a signal it belongs somewhere else.
Report outcome `blocked` with the reason rather than editing generated output.""",
    ),
)
"""The four notes `docs/05-devin-integration.md#knowledge-notes` lists, in that order.

The order is load-bearing: `DEVIN_KNOWLEDGE_IDS` is a positional record of which of these exist, so
a fifth note is appended and an existing one is edited in place — never reordered."""

SCHEDULE_NAME = "sentinel-nightly-vuln-sweep"
SCHEDULE_FREQUENCY = "0 3 * * *"
SCHEDULE_TAGS: tuple[str, ...] = (NAMESPACE_TAG, "class:scheduled-sweep")

SWEEP_PROMPT = """\
Run `pip-audit` and `npm audit` against {repo} on branch {branch}.

For each *new* finding that is not already tracked, open a GitHub issue on {repo} carrying the
`{label}` label and the class label that fits it:

- `class:{python_class}` for a Python dependency advisory (`pip-audit`);
- `class:{frontend_class}` for a JavaScript dependency advisory (`npm audit`).

Each issue names the package, the affected and the fixed version, the advisory id, and where the
dependency enters the tree.

Do not open duplicates. Search the repository's open and recently closed issues first, and skip any
finding that already has one.

Do not open a pull request and do not change any file. Filing the issue is the whole task: {repo}
is watched by an automated remediation pipeline that picks the issue up from the `{label}` label.
"""

# --- The `.env` record ----------------------------------------------------------------------------

KNOWLEDGE_IDS = "DEVIN_KNOWLEDGE_IDS"
SCHEDULE_ID = "DEVIN_SCHEDULE_ID"

_ASSIGNMENT = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")
"""One `.env` assignment. A commented-out line does not match, so `#DEVIN_SCHEDULE_ID=` is left
alone rather than resurrected as the line to overwrite."""

_UNSAFE_IN_VALUE = re.compile(r"""[\s#'"\\]""")
"""What must not appear in an id written into `.env`. Whitespace and `#` end an unquoted value, and
quotes and backslashes change how one is parsed — any of them would turn a rewrite of a file
holding real credentials into a corrupted one. An id containing one is reported to the operator
instead of written."""


class BootstrapError(RuntimeError):
    """A step that could not be completed, in terms an operator can act on.

    `make bootstrap-devin` is run by a human at a terminal, so a failure has to say which of the
    four steps stopped, what Devin actually answered and what to do about it. A traceback says none
    of those things.
    """

    def __init__(
        self, step: str, problem: str, remedy: str, command: str = "make bootstrap-devin"
    ) -> None:
        self.step = step
        self.problem = problem
        self.remedy = remedy
        # Which command the operator actually typed. The two modes of this file are two Makefile
        # targets, and a failure that names the one they did not run sends them to the wrong place.
        self.command = command
        super().__init__(f"{step}: {problem}")

    def report(self) -> str:
        return "\n".join(
            [
                f"{self.command} failed at {self.step}",
                f"  what happened:  {self.problem}",
                f"  what to do:     {self.remedy}",
            ]
        )


class EnvFile:
    """`.env`, as this script's record of what it has already created in the Devin organisation.

    Reading is per-variable and never returns the file, so there is no code path down which the
    credentials it holds could be printed.
    """

    def __init__(self, path: Path, environ: Mapping[str, str]) -> None:
        self.path = path
        self._environ = environ

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def recorded(self, name: str) -> str | None:
        """What the process would read for `name` — the environment first, then the file.

        The environment wins because that is the order `pydantic-settings` resolves in: a variable
        exported in the shell is what the worker will use, so it is also what tells this script the
        thing it names already exists.
        """
        for value in (self._environ.get(name), self._from_file(name)):
            if value is not None and value.strip():
                return value.strip()
        return None

    def bootstrapped_before(self) -> bool:
        """Whether this organisation has been through this script already.

        The schedule is created *after* the notes and recorded immediately, so a recorded schedule
        id cannot predate them: it means four notes were created and their record has since been
        lost. That is the difference between a first run and a `.env` that was replaced from
        `.env.example` — which is exactly what the missing-file remedy tells an operator to do.
        """
        return self.recorded(SCHEDULE_ID) is not None

    def recorded_ids(self, name: str) -> list[str]:
        """`name` decoded as the JSON array of ids it is documented to be."""
        raw = self.recorded(name)
        if raw is None:
            return []
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = None
        if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
            raise BootstrapError(
                "the .env record",
                f"{name} is not the JSON array of ids it is documented to be",
                f"fix or blank {name} in {self.path} — this script cannot tell what already "
                "exists from it, and creating the notes again would leave duplicates behind",
            )
        return list(decoded)

    def record(self, name: str, value: str) -> bool:
        """Write `name=value`, replacing the assignment in place. True if the file was rewritten.

        Returns False when the file already says this, so a re-run does not rewrite a file full of
        credentials in order to change nothing.
        """
        if not self.exists:
            raise _no_env_file(name, value)
        try:
            # `newline=""` turns off universal newlines, so a file written on Windows comes back
            # with its CRLF endings attached and goes back out with them. Reading it the default
            # way would rewrite every line ending in the file as a side effect of recording one id.
            text = self.path.read_text(encoding="utf-8", newline="")
            updated = _assign(text, name, value)
            if updated == text:
                return False
            _write_atomically(self.path, updated)
        except (OSError, UnicodeDecodeError) as exc:
            # A read-only directory, a full disk, a `.env` that is not UTF-8. The id is already
            # created at Devin by the time this runs, so the failure has to hand it back: an id
            # that reaches nobody belongs to a note nobody can find again.
            raise _unwritable(self.path, name, value, exc) from None
        return True

    def _from_file(self, name: str) -> str | None:
        if not self.exists:
            return None
        # The last assignment is the effective one, which is also the one `_assign` replaces. The
        # two must agree: reading the first while replacing the last would report a stale schedule
        # id as current and create a second sweep.
        found: str | None = None
        for line in self._text().splitlines():
            match = _ASSIGNMENT.match(line)
            if match and match.group(1) == name:
                found = _unquote(line[match.end() :])
        return found

    def _text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise BootstrapError(
                "the .env record",
                f"{self.path} could not be read: {_why(exc)}",
                "fix or replace the file, then re-run — nothing has been created yet",
            ) from None


def _assign(text: str, name: str, value: str) -> str:
    """`text` with `name` assigned `value` — the existing line rewritten, or a new line appended.

    Every other byte survives: comments, blank lines, ordering, the operator's own variables, and
    the line ending each line already had. Only the assignment that takes effect is replaced; a
    duplicate earlier in the file is left where it is, because dropping a line this script did not
    write would be an edit nobody asked for.
    """
    line = f"{name}={value}"
    lines = text.splitlines(keepends=True)
    target: int | None = None
    for index, raw in enumerate(lines):
        match = _ASSIGNMENT.match(raw)
        if match and match.group(1) == name:
            target = index
    if target is not None:
        raw = lines[target]
        ending = raw[len(raw.rstrip("\r\n")) :] or "\n"
        lines[target] = line + ending
        return "".join(lines)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return "".join([*lines, line, "\n"])


def _write_atomically(path: Path, text: str) -> None:
    """Replace `path` with `text`, all at once or not at all.

    The temporary file is created in the same directory, so `os.replace` is a rename within one
    filesystem and therefore atomic: an interruption leaves the previous `.env` complete rather
    than a truncated new one. Its name starts with `.env.`, which `.gitignore` already covers, so a
    crash cannot leave a file of credentials lying around untracked. The mode of the original is
    carried over rather than assumed — a `.env` an operator chmodded to 0600 stays 0600, and one
    that was not is not silently tightened either.

    A symlinked `.env` is followed rather than replaced. `os.replace` onto the link path would swap
    the link itself for a regular file: the real file would stop receiving updates while a second
    complete copy of every credential was left at the link.
    """
    target = path.resolve()
    mode = stat.S_IMODE(target.stat().st_mode)
    handle, name = tempfile.mkstemp(dir=target.parent, prefix=".env.", suffix=".tmp")
    temporary = Path(name)
    try:
        # newline="" so the line endings written are exactly the ones `_assign` preserved.
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


_QUOTED = re.compile(r"""^\s*(['"])(.*?)\1""")
_INLINE_COMMENT = re.compile(r"\s+#.*$")


def _unquote(value: str) -> str:
    """One `.env` value, read the way `dotenv` reads it.

    `dotenv` is what actually configures the process, so a value this script reads differently from
    `dotenv` is a value it would report to the operator as something the worker never sees.
    """
    quoted = _QUOTED.match(value)
    return quoted.group(2) if quoted else _INLINE_COMMENT.sub("", value).strip()


def _why(exc: OSError | UnicodeDecodeError) -> str:
    """Why a file operation failed, without quoting any of the file.

    `strerror` describes the operation, never the content. A `UnicodeDecodeError` renders the bytes
    it choked on, and those bytes came out of a file full of credentials, so it is not repeated.
    """
    return (exc.strerror or type(exc).__name__) if isinstance(exc, OSError) else "not valid UTF-8"


def _unwritable(
    path: Path, name: str, value: str, exc: OSError | UnicodeDecodeError
) -> BootstrapError:
    return BootstrapError(
        "the .env record",
        f"{name} could not be recorded in {path}: {_why(exc)}",
        f"add `{name}={value}` by hand once the file can be written, then re-run — what that line "
        "names has already been created, and without it the next run creates it a second time",
    )


def _checked_id(step: str, kind: str, value: str) -> str:
    """An id from Devin, confirmed safe to write into `.env` unquoted."""
    if not value or _UNSAFE_IN_VALUE.search(value):
        raise BootstrapError(
            step,
            f"Devin returned a {kind} id that cannot be written into .env safely: {value!r}",
            "record it by hand as one entry rather than letting this script corrupt a file "
            "holding credentials — the id is outside the character set an unquoted .env value "
            "can carry",
        )
    return value


def _no_env_file(name: str, value: str) -> BootstrapError:
    return BootstrapError(
        "the .env record",
        f"there is no file at the recording path to write {name} into, and none was created: a "
        "file holding nothing but this line would look like a configuration while missing every "
        "credential",
        f"run `cp .env.example .env`, fill it in, add `{name}={value}`, then re-run — until that "
        "line exists, this script cannot tell that these already exist and will create them again",
    )


# --- The report -----------------------------------------------------------------------------------

REACHABLE = "reachable"
"""Asked, and answered."""

DEGRADED = "degraded"
"""Asked, and **refused** — a `403` or a `404`, which `client.DEGRADES` calls a capability this
deployment does not have. Only this status claims a fallback, because only a refusal is an answer
the fallback is the right response to."""

FAULT = "fault"
"""Asked, and something went wrong that is not an answer: a `401`, a `500`, a dead connection, a
body that will not parse. The capability is left **unanswered** and no fallback is claimed —
recording "use the derived figures" would answer a question nobody asked."""

NOT_PROBED = "not probed"
"""Not asked, and why."""

REGISTERED = "registered"


@dataclass(frozen=True, slots=True)
class Capable:
    """One row of the capability table: what was probed, what came back, and what it means.

    `remedy` is set only on a `FAULT` row: a fault is the one outcome the probe cannot report and
    move on from, so it also carries what to do about it.
    """

    blocker: str
    name: str
    status: str
    detail: str
    remedy: str = ""


@dataclass
class Report:
    """What the run did — for the operator on stdout, and for the assertions in the tests."""

    steps: dict[str, str] = field(default_factory=dict)
    capabilities: list[Capable] = field(default_factory=list)

    def capability(self, name: str) -> Capable:
        return next(row for row in self.capabilities if row.name == name)


STEPS: tuple[str, ...] = ("token", "tags", "knowledge", "schedule")


def _label(step: str) -> str:
    return f"step {STEPS.index(step) + 1} of {len(STEPS)} ({step})"


REMEDIES: Mapping[int, str] = {
    401: (
        "DEVIN_API_TOKEN was rejected. Check the service-user token in .env — it starts with "
        "`cog_` — and that it belongs to the organisation named by DEVIN_ORG_ID."
    ),
    403: (
        "The service user lacks the permission this call needs. `ManageOrgSessions` at the "
        "organisation level is the minimum (docs/09-operations.md#prerequisites)."
    ),
    404: (
        "DEVIN_ORG_ID does not name an organisation this token can see, or this endpoint is not "
        "exposed to it. Check the id in the Devin dashboard."
    ),
    422: (
        "Devin rejected the body quoted above. docs/05-devin-integration.md is what it should "
        "match, so the mismatch is Sentinel's to fix rather than yours to work around."
    ),
    429: "Rate-limited even after the client's own retries. Wait, then re-run.",
}

GENERIC_REMEDY = (
    "Read the response above against docs/05-devin-integration.md, then re-run — every step is "
    "idempotent, so a re-run repeats nothing that already succeeded."
)

UNREACHABLE_REMEDY = (
    "Check DEVIN_API_BASE and the network, then re-run — every step is idempotent, so a re-run "
    "repeats nothing that already succeeded."
)

UNREADABLE_REMEDY = (
    "Devin accepted the request and answered with a body Sentinel does not recognise. The response "
    "shapes of the bootstrap endpoints are unverified (B8): compare the answer against "
    "docs/05-devin-integration.md and fix devin/schemas.py rather than this script."
)


def _diagnosis(exc: DevinError) -> tuple[str, str]:
    """What the client raised, as (what happened, what to do)."""
    if isinstance(exc, DevinAPIError):
        return (
            f"Devin answered {exc.status_code} to {exc.method} {exc.endpoint}: {exc.body}",
            REMEDIES.get(exc.status_code, GENERIC_REMEDY),
        )
    if isinstance(exc, DevinTransportError):
        return f"Devin could not be reached: {exc}", UNREACHABLE_REMEDY
    return str(exc), UNREADABLE_REMEDY


def _failed(step: str, exc: DevinError) -> BootstrapError:
    """Turn what the client raised into something an operator can act on."""
    problem, remedy = _diagnosis(exc)
    return BootstrapError(_label(step), problem, remedy)


# --- What a run changes ---------------------------------------------------------------------------


class Writer:
    """The one place this script changes anything, and therefore the whole of what `--dry-run` is.

    A run creates three things and records two of them: the tag vocabulary, the four knowledge
    notes and their ids in `.env`, the nightly sweep and its id in `.env`. Every one of those
    happens inside a coroutine handed to `_apply`, which on a dry run does not run it. So "a dry
    run writes nothing, to Devin or to `.env`" is one branch to read rather than a property to
    re-check at five call sites — the shape `scripts/file_remediation_issues.py` uses for the same
    reason.

    The `.env` record is inside the coroutine rather than beside it because there is nothing to
    record until the call that produces the id has returned. A dry run gets no ids, so there is
    nothing it could write even if the guard were removed from underneath it.

    Reads are not here. Listing sessions and the capability probes go straight to the client: they
    are what a preview is for.
    """

    def __init__(self, devin: DevinClient, env: EnvFile, *, dry_run: bool = False) -> None:
        self._devin = devin
        self.env = env
        self.dry_run = dry_run

    def __repr__(self) -> str:
        return f"Writer(env={str(self.env.path)!r}, dry_run={self.dry_run!r})"

    async def register_vocabulary(self) -> None:
        """`PUT` the whole vocabulary. Idempotent by nature, and a replacement of whatever is
        registered rather than an addition to it."""

        async def register() -> None:
            await self._devin.register_tags(VOCABULARY)

        await self._apply("tags", register)

    async def create_note(self, note: Note, recorded: list[str]) -> None:
        """Create one knowledge note and append its id to the record, or neither.

        `recorded` is the list `.env` holds, extended in place: the record is rewritten after
        *each* creation, because an id that was not recorded belongs to a note nobody can find
        again.
        """

        async def create() -> None:
            created = await self._devin.create_knowledge_note(
                name=note.name, body=note.body, trigger_description=note.trigger
            )
            recorded.append(_checked_id(_label("knowledge"), "knowledge note", created.id))
            self.env.record(KNOWLEDGE_IDS, json.dumps(recorded, separators=(",", ":")))

        await self._apply("knowledge", create)

    async def create_schedule(self, prompt: str) -> str | None:
        """Create the nightly sweep and record its id. `None` when nothing was created, which is
        what a dry run leaves behind."""

        async def create() -> str:
            schedule = await self._devin.create_schedule(
                name=SCHEDULE_NAME,
                prompt=prompt,
                frequency=SCHEDULE_FREQUENCY,
                tags=SCHEDULE_TAGS,
            )
            identifier = _checked_id(_label("schedule"), "schedule", schedule.id)
            self.env.record(SCHEDULE_ID, identifier)
            return identifier

        return await self._apply("schedule", create)

    async def _apply[T](self, step: str, mutate: Callable[[], Coroutine[Any, Any, T]]) -> T | None:
        """Make one change, or — on a dry run — none of it.

        The coroutine is never created on a dry run, so nothing is sent and nothing is written.
        `None` means exactly that: nothing happened, and there is no id for a caller to report.
        """
        if self.dry_run:
            return None
        try:
            return await mutate()
        except DevinError as exc:
            raise _failed(step, exc) from None


# --- The steps ------------------------------------------------------------------------------------


async def _verify_token(devin: DevinClient) -> str:
    """List the organisation's sessions, which is what proves the token and the org id agree.

    First rather than last. The spec's bullet list puts the verification at the end, but the point
    of it is to catch a rejected token, and catching one after three notes have been created leaves
    three notes to remove by hand.
    """
    try:
        sessions = await devin.list_sessions()
    except DevinError as exc:
        raise _failed("token", exc) from None
    ours = sum(1 for session in sessions if NAMESPACE_TAG in session.tags)
    return f"accepted — {len(sessions)} session(s) visible, {ours} of them Sentinel's"


async def _register_vocabulary(writer: Writer) -> str:
    """Register the tag vocabulary. Idempotent because it is a `PUT` of the whole set.

    Which is also why a dry run cannot say which of them are new. The `PUT` replaces the
    organisation's vocabulary with what it carries, and v3 exposes no read of the current one —
    `docs/05-devin-integration.md#endpoints-used` has this path under `PUT` and nothing else, so
    there is nothing to compare against. The preview names the whole set and says what sending it
    means; a tag registered outside this list, by hand in the dashboard, is what a run would drop.
    """
    await writer.register_vocabulary()
    listed = ", ".join(VOCABULARY)
    if writer.dry_run:
        return f"would replace the whole vocabulary with these {len(VOCABULARY)}: {listed}"
    return f"registered {len(VOCABULARY)} tags: {listed}"


async def _seed_knowledge(writer: Writer) -> str:
    """Create whichever of the four notes `.env` does not already record the id of.

    `DEVIN_KNOWLEDGE_IDS` is positional — the *n*-th id belongs to the *n*-th entry of `NOTES` — so
    a run that stopped after two notes resumes at the third instead of creating four more. No v3
    endpoint lists an organisation's knowledge notes, so this record is the only thing standing
    between a second run and a fifth note.

    The record is written after *each* creation rather than once at the end: an id that was not
    recorded belongs to a note nobody can find again, and the point of the record is that a failure
    part-way through costs nothing on the next run.

    So what `.env` records is the whole of what decides between "create" and "skip", and it is what
    a dry run reports: four notes recorded is a run that would create nothing, and a blank record
    beside four notes that exist is the duplication this step refuses to make.
    """
    env = writer.env
    recorded = env.recorded_ids(KNOWLEDGE_IDS)
    if not recorded and env.bootstrapped_before():
        # A blank `DEVIN_KNOWLEDGE_IDS` beside a recorded schedule is not a first run: the schedule
        # is created after the notes, so it cannot exist without them. The likely cause is exactly
        # what `_no_env_file` tells an operator to do — `cp .env.example .env`, which ships this
        # variable blank — and creating four more notes on top of four that already exist would be
        # a silent duplication nothing later can detect.
        raise BootstrapError(
            _label("knowledge"),
            f"{KNOWLEDGE_IDS} is empty but {SCHEDULE_ID} is recorded, so this organisation has "
            "been bootstrapped before and its four notes still exist; creating them again would "
            "leave two sets that nothing can tell apart",
            f"restore {KNOWLEDGE_IDS} from the run that wrote it (the ids are on that run's "
            f"output), or — if the notes really are gone — clear {SCHEDULE_ID} as well and re-run "
            "to start from nothing",
        )
    kept = len(recorded)
    outstanding = NOTES[kept:]
    if not outstanding:
        already = f"{kept} note(s) already recorded"
        if writer.dry_run:
            return f"{already} in {KNOWLEDGE_IDS} — none would be created"
        return f"{already} — nothing created"

    for note in outstanding:
        await writer.create_note(note, recorded)
    if writer.dry_run:
        return (
            f"would create {len(outstanding)} note(s) and record the id(s); "
            f"{KNOWLEDGE_IDS} records {kept} of {len(NOTES)}"
        )
    already = f", {kept} already recorded" if kept else ""
    return f"created {len(outstanding)} note(s){already}; {KNOWLEDGE_IDS} written to {env.path}"


async def _create_schedule(writer: Writer, settings: TargetSettings) -> str:
    """Create the nightly sweep, unless `.env` records that it already exists.

    The same shape as the notes, for the same reason: `POST /schedules` creates one every time it
    is called and v3 offers nothing that lists them, so a second run would leave two sweeps filing
    the same issues every night.

    It is also the step a preview is most worth running for. What it creates keeps acting on its
    own every night, and `.env` is the only thing that stops a second one being made.
    """
    env = writer.env
    existing = env.recorded(SCHEDULE_ID)
    if existing:
        if writer.dry_run:
            return f"already recorded in {SCHEDULE_ID} ({existing}) — none would be created"
        return f"already created ({existing}) — nothing created"
    prompt = SWEEP_PROMPT.format(
        repo=settings.target_repo,
        branch=settings.target_base_branch,
        label=settings.autofix_label,
        python_class=IssueClass.SECURITY_DEP.value,
        frontend_class=IssueClass.FRONTEND_DEP.value,
    )
    created = await writer.create_schedule(prompt)
    if created is None:
        return (
            f"would create {SCHEDULE_NAME} at {SCHEDULE_FREQUENCY} UTC — a recurring sweep; "
            f"{SCHEDULE_ID} records nothing"
        )
    return (
        f"created {SCHEDULE_NAME} ({created}) at {SCHEDULE_FREQUENCY} UTC; "
        f"{SCHEDULE_ID} written to {env.path}"
    )


# --- The probe ------------------------------------------------------------------------------------


async def _probe(devin: DevinClient, settings: DevinSettings) -> list[Capable]:
    """Ask each optional capability whether this deployment can have it.

    Nothing here raises, but not everything here is an answer. A **refusal** — the `403` and `404`
    that `client.DEGRADES` recognises — is what the degradation table in
    `docs/05-devin-integration.md` is for, and reporting it with its fallback is the whole purpose
    of the probe. A **fault** is not a refusal: `client.py` re-raises `401` precisely so that a
    rejected token is not silently turned into "use the derived figures", and turning that re-raise
    back into a fallback here would undo the distinction one layer down. Those become `FAULT` rows,
    which claim no fallback and which `bootstrap` refuses to exit 0 on.
    """
    return [
        await _probe_session_metrics(devin),
        await _probe_acu_spend(devin),
        _probe_playbook_creation(settings),
    ]


async def _probe_session_metrics(devin: DevinClient) -> Capable:
    capability = Capability.SESSION_METRICS
    try:
        metrics = await devin.session_metrics()
    except DevinError as exc:
        return _fault("B5", capability, exc)
    if not metrics.available:
        if metrics.reason is Unavailability.NOT_CONFIGURED:
            # Not asked, so B5 is not answered. The question B5 puts is whether the *token* carries
            # `ViewAccountMetrics`; an unset DEVIN_ENTERPRISE_ID only says the panel is switched
            # off. Reporting that as `degraded` would let an operator close B5 on a run that made
            # no request at all.
            return Capable(
                "B5",
                capability.value,
                NOT_PROBED,
                "DEVIN_ENTERPRISE_ID is unset, so nothing was asked and B5 stays open — set it and "
                f"re-run to find out. Until then: {capability.fallback}",
            )
        return _refused("B5", capability, metrics.reason.value, metrics.status_code)
    figures = metrics.value
    return Capable(
        "B5",
        capability.value,
        REACHABLE,
        f"{figures.sessions_with_merged_prs_count} session(s) with merged PRs, "
        f"{figures.avg_acus_per_session:.2f} ACU per session — the panel uses Devin's own figures",
    )


async def _probe_acu_spend(devin: DevinClient) -> Capable:
    capability = Capability.ACU_SPEND
    try:
        spend = await devin.daily_consumption()
    except DevinError as exc:
        return _fault("—", capability, exc)
    if not spend.available:
        return _refused("—", capability, spend.reason.value, spend.status_code)
    consumption = spend.value
    return Capable(
        "—",
        capability.value,
        REACHABLE,
        f"{len(consumption.days)} day(s) reported, {consumption.total_acus:.2f} ACU in total — "
        "the budget guard has Devin's own accounting",
    )


def _probe_playbook_creation(settings: DevinSettings) -> Capable:
    """Report B6 without probing it, and say why.

    `POST /v3/enterprise/playbooks` is deliberately absent from the endpoint table in
    `docs/05-devin-integration.md`, and therefore from the client, because the four playbooks are
    created in the Devin UI (B6). Probing it would mean either creating a playbook the
    organisation would then have to remove, or reaching an endpoint no other part of Sentinel can
    reach. What is worth reporting is whether the fallback that replaces it is actually in place:
    the ids in `DEVIN_PLAYBOOK_IDS`.
    """
    scope = (
        "DEVIN_ENTERPRISE_ID is unset, so nothing asked Devin about enterprise scope and B6 stays "
        "open with B5"
        if settings.devin_enterprise_id is None
        else "enterprise scope claimed — session_metrics above is whether the token carries it"
    )
    return Capable(
        "B6",
        Capability.PLAYBOOK_CREATION.value,
        NOT_PROBED,
        f"{scope}; fallback in place: {len(settings.devin_playbook_ids)} playbook id(s) "
        f"configured in DEVIN_PLAYBOOK_IDS -> {Capability.PLAYBOOK_CREATION.fallback}",
    )


def _refused(blocker: str, capability: Capability, reason: str, status_code: int | None) -> Capable:
    """Devin was asked and said no. Reported with the fallback that replaces it — the sentence the
    dashboard labels the derived figure with, taken from the same enum so the two cannot drift."""
    status = f" ({status_code})" if status_code is not None else ""
    return Capable(
        blocker, capability.value, DEGRADED, f"{reason}{status} -> {capability.fallback}"
    )


def _fault(blocker: str, capability: Capability, exc: DevinError) -> Capable:
    """Devin was asked and something went wrong that is not an answer.

    Everything the client hands back here is a fault by construction: it converts a `403` and a
    `404` into `Unavailable` itself and raises everything else, `401` most pointedly, because "a
    rejected token is a misconfiguration that must be fixed, and silently falling back to derived
    figures would hide it".

    So the row says the capability is unanswered and names no fallback. It carries the remedy for
    whatever went wrong, which `bootstrap` raises with once the whole table has been printed.
    """
    problem, remedy = _diagnosis(exc)
    return Capable(
        blocker,
        capability.value,
        FAULT,
        f"unanswered — {problem}",
        remedy=remedy,
    )


def _table(rows: Sequence[Capable]) -> list[str]:
    blocker = max(len(row.blocker) for row in rows)
    name = max(len(row.name) for row in rows)
    status = max(len(row.status) for row in rows)
    return [
        f"  {row.blocker:<{blocker}}  {row.name:<{name}}  {row.status:<{status}}  {row.detail}"
        for row in rows
    ]


# --- The lookup -----------------------------------------------------------------------------------

PLAYBOOK_NAMES: tuple[str, ...] = tuple(playbook.name for playbook in PLAYBOOKS)
"""The four names `docs/playbooks/README.md` tells an operator to give the playbooks in the Devin
UI, which are also the keys `DEVIN_PLAYBOOK_IDS` is written under: four entries resolve all eight
issue classes (`docs/adr/2026-08-08-playbook-ids-keyed-by-class-or-name.md`)."""

PLAYBOOK_IDS = "DEVIN_PLAYBOOK_IDS"
"""Unlike `KNOWLEDGE_IDS` and `SCHEDULE_ID` above, this one is printed and never written: the
listing matches playbooks to names by title, which is not a match worth editing a file of
credentials on."""

PASTE_HEADING = f"{PLAYBOOK_IDS} for the four playbooks of docs/playbooks/ — paste this into .env:"


def _resolved(page: PlaybookPage) -> tuple[dict[str, str], list[str]]:
    """The four playbooks of `docs/playbooks/` resolved to ids, and what could not be resolved.

    Matched on the title, ignoring case and surrounding space, because the title is what somebody
    typed into the Devin UI and `docs/playbooks/README.md` is what told them what to type.

    A name matching *two* playbooks is left out rather than guessed at. Writing one of two ids into
    `.env` would point a whole issue class at the wrong playbook, and neither Sentinel nor Devin
    would report anything wrong about it — the sessions would simply be run under instructions
    nobody chose.
    """
    by_title: dict[str, list[str]] = {}
    for playbook in page.playbooks:
        by_title.setdefault(playbook.title.strip().casefold(), []).append(playbook.playbook_id)
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for name in PLAYBOOK_NAMES:
        found = by_title.get(name.casefold(), [])
        if len(found) == 1:
            resolved[name] = found[0]
        elif not found:
            unresolved.append(f"{name} — no playbook of this organisation carries that title")
        else:
            ids = ", ".join(found)
            unresolved.append(f"{name} — {len(found)} playbooks carry that title: {ids}")
    return resolved, unresolved


def _listing(page: PlaybookPage) -> list[str]:
    """Every playbook the organisation holds: its title, its id, and the scope it was created at.

    `access_type` is printed because it is the one thing here that speaks to B6 — a playbook
    reported as `enterprise` was created at a scope this organisation's own token may not be able
    to write to, which is worth knowing before somebody tries to edit it through the API.
    """
    width = max(len(playbook.title) for playbook in page.playbooks)
    return [
        f"  {playbook.title:<{width}}  {playbook.playbook_id}"
        + (f"  ({playbook.access_type})" if playbook.access_type else "")
        for playbook in page.playbooks
    ]


async def list_playbooks(devin: DevinClient, settings: DevinSettings, *, out: TextIO) -> Capable:
    """Print the organisation's playbooks, and the `DEVIN_PLAYBOOK_IDS` they make up.

    Read-only, and the only thing in this file that is: the four steps above register and create,
    this asks a question. Nothing is sent but a `GET`, and `.env` is not opened at all — the ids are
    printed for the operator to paste, because a run that rewrote `.env` from a list it had just
    matched by title would be a write made on a guess.

    A **refusal** is the likely answer if this service user does not carry the playbook permission,
    and it is reported in the one line `_refused` builds, carrying the fallback that replaces it:
    open each playbook in the web app and read its id from the page. That exits 0, because it is an
    answer. A **fault** is not, and raises once the row has been printed — the same distinction the
    probe above draws, for the same reason.
    """
    capability = Capability.PLAYBOOK_DISCOVERY
    page: PlaybookPage | None = None
    try:
        result = await devin.list_playbooks()
    except DevinError as exc:
        row = _fault("B6", capability, exc)
    else:
        if result.available:
            page = result.value
            more = (
                "; this is the first page, the organisation has more" if page.has_next_page else ""
            )
            row = Capable(
                "B6",
                capability.value,
                REACHABLE,
                f"{len(page.playbooks)} playbook(s) visible{more}",
            )
        else:
            row = _refused("B6", capability, result.reason.value, result.status_code)

    lines = [
        f"Devin playbooks — organisation {settings.devin_org_id} at {settings.devin_api_base}",
        "",
        *_table([row]),
    ]
    if page is not None:
        resolved, unresolved = _resolved(page)
        if page.playbooks:
            lines += ["", *_listing(page)]
        if unresolved:
            lines += ["", *(f"  not matched: {problem}" for problem in unresolved)]
        lines += [
            "",
            PASTE_HEADING,
            "",
            f"  {PLAYBOOK_IDS}={json.dumps(resolved, separators=(',', ':'))}",
        ]
    print("\n".join(lines), file=out)

    # After the row, not instead of it — as in the probe above. A fault leaves the question
    # unanswered, so it must not exit 0; a refusal is an answer and does.
    if row.status == FAULT:
        raise BootstrapError(
            "the playbook listing", row.detail, row.remedy, command="make devin-playbooks"
        )
    return row


# --- The run --------------------------------------------------------------------------------------

CAPABILITY_HEADING = "\nOptional capabilities — the degradation path, before the demo not during it"

DRY_RUN_HEADING = " (dry run — nothing is created, and .env is not written)"

LIMITS_HEADING = "\nWhat this preview cannot tell you"

DRY_RUN_FOOTER = (
    "\nNothing was written: no request that changes anything was sent, and {path} was not touched. "
    "The token was verified and the capabilities were probed, because those are reads. Re-run "
    "without --dry-run to create what the four steps describe."
)


DRY_RUN_LIMITS: tuple[str, ...] = (
    f"which of the {len(VOCABULARY)} tags are new. The PUT replaces the organisation's vocabulary "
    "as a whole and v3 exposes no read of the current one, so a tag somebody added in the "
    "dashboard is dropped without this run naming it.",
    f"whether the notes and the sweep really exist. {KNOWLEDGE_IDS} and {SCHEDULE_ID} are the only "
    "record — nothing in v3 lists either — so what these steps would skip is what the file says, "
    "not what the organisation holds.",
    "whether Devin enforces the vocabulary at all (B7). That shows only when a session is created "
    "with a tag outside it, which neither this preview nor the run does.",
)
"""The three things a preview is honestly not able to answer.

Printed rather than left to be discovered: each is a way the real run could do something the report
above does not show, and an operator deciding whether to type the command without `--dry-run` is
owed the shape of what they cannot see."""


async def bootstrap(
    devin: DevinClient,
    settings: DevinSettings,
    *,
    env: EnvFile,
    out: TextIO,
    dry_run: bool = False,
) -> Report:
    """The four steps and the probe, printing as it goes. Raises `BootstrapError` on a step.

    `dry_run` reaches exactly one place — the `Writer` the three creating steps go through — so
    what a preview reports is what a run would do, produced by the same code that would do it.
    """
    report = Report()
    writer = Writer(devin, env, dry_run=dry_run)
    print(
        f"Devin bootstrap — organisation {settings.devin_org_id} at {settings.devin_api_base}"
        f"{DRY_RUN_HEADING if dry_run else ''}\n",
        file=out,
    )

    def done(step: str, summary: str) -> None:
        # Printed as each step finishes rather than in one block at the end: the steps make network
        # calls, and an operator watching a run that stops needs to see where it got to.
        report.steps[step] = summary
        print(f"  [{STEPS.index(step) + 1}/{len(STEPS)}] {step:<10} {summary}", file=out)

    done("token", await _verify_token(devin))
    done("tags", await _register_vocabulary(writer))
    done("knowledge", await _seed_knowledge(writer))
    done("schedule", await _create_schedule(writer, settings))

    report.capabilities.extend(await _probe(devin, settings))
    report.capabilities.append(_vocabulary_row(dry_run))
    print(CAPABILITY_HEADING, file=out)
    print("\n".join(_table(report.capabilities)), file=out)
    if dry_run:
        print(LIMITS_HEADING, file=out)
        print("\n".join(f"  - {limit}" for limit in DRY_RUN_LIMITS), file=out)
        print(DRY_RUN_FOOTER.format(path=env.path), file=out)

    # After the table, not instead of it. The four steps are done and the rest of the answer is
    # worth having, but a capability the probe could not put a question to is a run that did not do
    # what `docs/09-operations.md` asked of it, and exiting 0 would have it recorded as an answer.
    faults = [row for row in report.capabilities if row.status == FAULT]
    if faults:
        raise BootstrapError(
            "the capability probe",
            "; ".join(f"{row.name}: {row.detail}" for row in faults),
            f"{faults[0].remedy} This is a fault rather than a refusal, so no fallback is claimed "
            "and the capability is still an open question. "
            + (
                "Nothing was created, here or above: this was a dry run."
                if dry_run
                else "The four setup steps above are done and idempotent: re-running repeats none "
                "of them."
            ),
        )
    return report


def _vocabulary_row(dry_run: bool) -> Capable:
    """B7's row: what step 2 did about the vocabulary, which on a dry run is nothing.

    Reporting `registered` after a run that sent no `PUT` would close B7's mitigation on a preview.
    """
    if dry_run:
        return Capable(
            "B7",
            "tag_vocabulary",
            NOT_PROBED,
            f"{len(VOCABULARY)} tags would be registered; nothing was sent, so the vocabulary is "
            "whatever it already was",
        )
    return Capable(
        "B7",
        "tag_vocabulary",
        REGISTERED,
        f"{len(VOCABULARY)} tags accepted; whether Devin enforces the vocabulary shows only at "
        "session creation, which this script does not do",
    )


async def _run(settings: DevinSettings, env: EnvFile, out: TextIO, *, dry_run: bool) -> Report:
    async with DevinClient(settings) as devin:
        return await bootstrap(devin, settings, env=env, out=out, dry_run=dry_run)


async def _lookup(settings: DevinSettings, out: TextIO) -> Capable:
    async with DevinClient(settings) as devin:
        return await list_playbooks(devin, settings, out=out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Register the tag vocabulary, knowledge notes and nightly sweep in the Devin "
        "organisation, then report which optional endpoints this deployment can reach."
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help="the file the created ids are recorded in (default: .env)",
    )
    # Two ways of not doing the run, and neither is a modifier of the other: `--list-playbooks`
    # already creates nothing, so `--dry-run` alongside it would describe a property it has anyway.
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="report what the run would create and write nothing — no request that changes "
        "anything, and no line in .env. Step 1 and the capability probes still run: they are reads",
    )
    mode.add_argument(
        "--list-playbooks",
        action="store_true",
        help="instead of the run above: list the organisation's playbooks and print the "
        f"{PLAYBOOK_IDS} to paste into .env. Creates nothing and writes to no file",
    )
    arguments = parser.parse_args(argv)

    # The lookup is what an operator runs *before* they have DEVIN_PLAYBOOK_IDS, so it must not be
    # among the variables demanded of them.
    try:
        settings = load_config(PlaybookLookup if arguments.list_playbooks else Configuration)
    except ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 2
    # Diagnostics to stderr, so that the report on stdout stays the report.
    configure_logging(settings, stream=sys.stderr)

    try:
        if arguments.list_playbooks:
            asyncio.run(_lookup(settings, sys.stdout))
        else:
            asyncio.run(
                _run(
                    settings,
                    EnvFile(arguments.env, os.environ),
                    sys.stdout,
                    dry_run=arguments.dry_run,
                )
            )
    except BootstrapError as exc:
        print(exc.report(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
