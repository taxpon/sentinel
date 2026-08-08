#!/usr/bin/env python3
"""One-time setup of the Devin organisation, as `docs/09-operations.md#bootstrap` specifies it.

    make bootstrap-devin

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from sentinel.config import ConfigurationError, Settings, get_settings
from sentinel.devin.client import (
    DevinAPIError,
    DevinClient,
    DevinError,
    DevinTransportError,
)
from sentinel.devin.playbooks import NAMESPACE_TAG, TAG_PREFIXES, IssueClass
from sentinel.devin.schemas import Capability, Unavailability
from sentinel.observability.logging import configure_logging

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

    def __init__(self, step: str, problem: str, remedy: str) -> None:
        self.step = step
        self.problem = problem
        self.remedy = remedy
        super().__init__(f"{step}: {problem}")

    def report(self) -> str:
        return "\n".join(
            [
                f"make bootstrap-devin failed at {self.step}",
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


async def _register_vocabulary(devin: DevinClient) -> str:
    """Register the tag vocabulary. Idempotent because it is a `PUT` of the whole set."""
    try:
        await devin.register_tags(VOCABULARY)
    except DevinError as exc:
        raise _failed("tags", exc) from None
    return f"registered {len(VOCABULARY)} tags: {', '.join(VOCABULARY)}"


async def _seed_knowledge(devin: DevinClient, env: EnvFile) -> str:
    """Create whichever of the four notes `.env` does not already record the id of.

    `DEVIN_KNOWLEDGE_IDS` is positional — the *n*-th id belongs to the *n*-th entry of `NOTES` — so
    a run that stopped after two notes resumes at the third instead of creating four more. No v3
    endpoint lists an organisation's knowledge notes, so this record is the only thing standing
    between a second run and a fifth note.

    The record is written after *each* creation rather than once at the end: an id that was not
    recorded belongs to a note nobody can find again, and the point of the record is that a failure
    part-way through costs nothing on the next run.
    """
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
    outstanding = NOTES[len(recorded) :]
    if not outstanding:
        return f"{len(recorded)} note(s) already recorded — nothing created"

    kept = len(recorded)
    for note in outstanding:
        try:
            created = await devin.create_knowledge_note(
                name=note.name, body=note.body, trigger_description=note.trigger
            )
        except DevinError as exc:
            raise _failed("knowledge", exc) from None
        recorded.append(_checked_id(_label("knowledge"), "knowledge note", created.id))
        env.record(KNOWLEDGE_IDS, json.dumps(recorded, separators=(",", ":")))
    already = f", {kept} already recorded" if kept else ""
    return f"created {len(outstanding)} note(s){already}; {KNOWLEDGE_IDS} written to {env.path}"


async def _create_schedule(devin: DevinClient, settings: Settings, env: EnvFile) -> str:
    """Create the nightly sweep, unless `.env` records that it already exists.

    The same shape as the notes, for the same reason: `POST /schedules` creates one every time it
    is called and v3 offers nothing that lists them, so a second run would leave two sweeps filing
    the same issues every night.
    """
    existing = env.recorded(SCHEDULE_ID)
    if existing:
        return f"already created ({existing}) — nothing created"
    prompt = SWEEP_PROMPT.format(
        repo=settings.target_repo,
        branch=settings.target_base_branch,
        label=settings.autofix_label,
        python_class=IssueClass.SECURITY_DEP.value,
        frontend_class=IssueClass.FRONTEND_DEP.value,
    )
    try:
        schedule = await devin.create_schedule(
            name=SCHEDULE_NAME,
            prompt=prompt,
            frequency=SCHEDULE_FREQUENCY,
            tags=SCHEDULE_TAGS,
        )
    except DevinError as exc:
        raise _failed("schedule", exc) from None
    env.record(SCHEDULE_ID, _checked_id(_label("schedule"), "schedule", schedule.id))
    return (
        f"created {SCHEDULE_NAME} ({schedule.id}) at {SCHEDULE_FREQUENCY} UTC; "
        f"{SCHEDULE_ID} written to {env.path}"
    )


# --- The probe ------------------------------------------------------------------------------------


async def _probe(devin: DevinClient, settings: Settings) -> list[Capable]:
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


def _probe_playbook_creation(settings: Settings) -> Capable:
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


# --- The run --------------------------------------------------------------------------------------

CAPABILITY_HEADING = "\nOptional capabilities — the degradation path, before the demo not during it"


async def bootstrap(
    devin: DevinClient,
    settings: Settings,
    *,
    env: EnvFile,
    out: TextIO,
) -> Report:
    """The four steps and the probe, printing as it goes. Raises `BootstrapError` on a step."""
    report = Report()
    print(
        f"Devin bootstrap — organisation {settings.devin_org_id} at {settings.devin_api_base}\n",
        file=out,
    )

    def done(step: str, summary: str) -> None:
        # Printed as each step finishes rather than in one block at the end: the steps make network
        # calls, and an operator watching a run that stops needs to see where it got to.
        report.steps[step] = summary
        print(f"  [{STEPS.index(step) + 1}/{len(STEPS)}] {step:<10} {summary}", file=out)

    done("token", await _verify_token(devin))
    done("tags", await _register_vocabulary(devin))
    done("knowledge", await _seed_knowledge(devin, env))
    done("schedule", await _create_schedule(devin, settings, env))

    report.capabilities.extend(await _probe(devin, settings))
    report.capabilities.append(
        Capable(
            "B7",
            "tag_vocabulary",
            REGISTERED,
            f"{len(VOCABULARY)} tags accepted; whether Devin enforces the vocabulary shows only at "
            "session creation, which this script does not do",
        )
    )
    print(CAPABILITY_HEADING, file=out)
    print("\n".join(_table(report.capabilities)), file=out)

    # After the table, not instead of it. The four steps are done and the rest of the answer is
    # worth having, but a capability the probe could not put a question to is a run that did not do
    # what `docs/09-operations.md` asked of it, and exiting 0 would have it recorded as an answer.
    faults = [row for row in report.capabilities if row.status == FAULT]
    if faults:
        raise BootstrapError(
            "the capability probe",
            "; ".join(f"{row.name}: {row.detail}" for row in faults),
            f"{faults[0].remedy} This is a fault rather than a refusal, so no fallback is claimed "
            "and the capability is still an open question. The four setup steps above are done and "
            "idempotent: re-running repeats none of them.",
        )
    return report


async def _run(settings: Settings, env: EnvFile, out: TextIO) -> Report:
    async with DevinClient(settings) as devin:
        return await bootstrap(devin, settings, env=env, out=out)


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
    arguments = parser.parse_args(argv)

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 2
    # Diagnostics to stderr, so that the report on stdout stays the report.
    configure_logging(settings, stream=sys.stderr)

    try:
        asyncio.run(_run(settings, EnvFile(arguments.env, os.environ), sys.stdout))
    except BootstrapError as exc:
        print(exc.report(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
