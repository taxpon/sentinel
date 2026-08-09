#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Check that this repository is safe to make public, and say what a human still has to judge.

    uv run scripts/audit_history.py

Mechanises the "Before making the repository public" checklist of `docs/09-operations.md`, which is
how [B13](../docs/blockers.md) is discharged before [B12](../docs/blockers.md) is acted on. Four
checks, in the order the checklist states them:

1. **The assignment brief has not leaked.** `requirements/` and `CLAUDE.local.md` are git-ignored
   and hold the brief. Six-word windows of them, taken at a stride of three, are looked for
   everywhere in the history.
2. **No credential-shaped string is in the history** — `cog_…`, `github_pat_…`, `ghp_…`, `whsec_…`.
3. **The git-ignored files were never committed**, on any branch, at any point.
4. **They are still ignored**, so that a later edit to `.gitignore` cannot quietly undo (3).

**This script is read-only and makes no network call.** It does not change the repository's
visibility and never will: publishing is the operator's decision, and a script that could take it
would be a script nobody could safely re-run.

**It never prints the brief, and never prints a secret.** What it reports is counts, paths, commit
SHAs — and, for the phrase check only, the six words that matched, because a human cannot tell an
accidental collision from a leak without seeing them. Six words of the brief are already in the
history by the time they are reported; the words around them are not, and are not printed.

Exit status: `0` clean, `1` something needs a human's eye, `2` a check could not run at all. A check
that cannot run is not a check that passed — a fresh clone has no `requirements/`, so check 1 fails
loudly there rather than reporting nothing found.

**Why the history is read as `git log --all -p` and not as the working tree.** Everything here is
about what a reader of the published repository could recover, and `git clone` hands them every
commit on every branch. Merges are read against their first parent (`--diff-merges=first-parent`)
rather than skipped, which is `git log -p`'s default: a conflict resolved by hand exists in the
merge commit and in neither parent, so the default would step straight over the one commit whose
content nobody reviewed. Commit messages are read too, since `git log -p` shows them and a brief
pasted into one is as public as a brief pasted into a file.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

BRIEF_SOURCES: Final[tuple[str, ...]] = ("requirements", "CLAUDE.local.md")
"""Where the assignment brief lives. Git-ignored, and the subject of check 1."""

IGNORED_PATHS: Final[tuple[str, ...]] = ("requirements/", "CLAUDE.local.md", ".env")
"""The paths checks 3 and 4 are about, as `docs/09-operations.md` lists them. A trailing slash
means "this directory and everything under it"."""

CREDENTIAL_PREFIXES: Final[tuple[str, ...]] = ("cog_", "github_pat_", "ghp_", "whsec_")
"""Devin token, GitHub fine-grained PAT, GitHub classic PAT, webhook secret."""

CREDENTIAL_TAIL: Final = 4
"""How much has to follow a prefix before it counts as credential-shaped.

`cog_` on its own appears in prose in `docs/02-architecture.md` and `docs/blockers.md`, naming the
shape of the token rather than carrying one. Four characters is enough to drop those and far short
of any real credential."""

PHRASE_WORDS: Final = 6
PHRASE_STRIDE: Final = 3
"""Six-word windows every three words. Six is long enough that ordinary English rarely repeats it
and short enough to survive a re-wrap or a lightly edited quotation; the stride of three means a
copied sentence produces several overlapping hits rather than one that could be luck."""

FIXTURE_ROOT: Final = "tests/"
"""Where a credential-shaped string is expected rather than alarming. The suite needs strings shaped
like the real ones — that is what the redaction patterns are written against — so the check reports
*where* every match lives and only fails on the ones outside here."""

COMMIT_MESSAGE: Final = "(commit message)"
"""The pseudo-path a match in a commit message is reported under."""

EXIT_FINDINGS: Final = 1
EXIT_CANNOT_RUN: Final = 2

WORD: Final = re.compile(r"[A-Za-z0-9']+")
CREDENTIAL: Final = re.compile(
    "(" + "|".join(CREDENTIAL_PREFIXES) + ")" + f"[A-Za-z0-9_]{{{CREDENTIAL_TAIL},}}"
)


class AuditError(RuntimeError):
    """A check could not be carried out. Never confused with a check that found nothing."""


class Status(StrEnum):
    OK = "OK"
    FAIL = "FAIL"
    UNRUN = "CANNOT RUN"


@dataclass(frozen=True, slots=True)
class Check:
    """One line of the checklist, and what running it produced."""

    number: int
    title: str
    status: Status
    summary: str
    detail: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Source:
    """A piece of text the history carries, and where a reader would find it."""

    commit: str
    path: str
    text: str

    def where(self) -> str:
        return f"{self.path} @ {self.commit[:8]}"


# --- The history ----------------------------------------------------------------------------------


def git(root: Path, *args: str) -> str:
    """One read-only git command, or an `AuditError` naming it.

    Nothing here writes, so there is no argument to guard against; the value of routing every call
    through one place is that a git that is missing, or a directory that is not a repository, is
    reported as "the check could not run" rather than as "nothing was found".
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise AuditError(f"git could not be run: {exc}") from None
    if result.returncode != 0:
        raise AuditError(f"git {' '.join(args)} failed: {' '.join(result.stderr.split())}")
    return result.stdout


def repository_root(start: Path) -> Path:
    return Path(git(start, "rev-parse", "--show-toplevel").strip())


def history(root: Path) -> list[Source]:
    """Every piece of text any commit on any branch introduced: added lines, and messages."""
    return [*commit_messages(root), *added_lines(root)]


def commit_messages(root: Path) -> Iterator[Source]:
    for record in git(root, "log", "--all", "--format=%H%x1f%B%x1e").split("\x1e"):
        commit, separator, message = record.strip("\n").partition("\x1f")
        if separator:
            yield Source(commit=commit, path=COMMIT_MESSAGE, text=message)


def added_lines(root: Path) -> list[Source]:
    return _parse_diffs(
        git(
            root,
            "-c",
            "core.quotePath=false",
            "log",
            "--all",
            "-p",
            "-U0",
            "--no-color",
            "--no-ext-diff",
            "--diff-merges=first-parent",
            "--format=%x1f%H",
        )
    )


def _parse_diffs(stream: str) -> list[Source]:
    """The added lines of `git log -p`, grouped by the commit and file they were added to.

    Whether a `+++ ` line is a file header or an added line beginning with `++` is decided by
    whether a hunk is open, which is the only thing that tells them apart. Getting that wrong would
    attribute a file's content to the previous path — and the paths are what makes a test fixture
    distinguishable from a leak in the report below.
    """
    sources: list[Source] = []
    commit = ""
    path: str | None = None
    in_hunk = False
    added: list[str] = []

    def close() -> None:
        if path is not None and added:
            sources.append(Source(commit=commit, path=path, text="\n".join(added)))
        added.clear()

    for line in stream.splitlines():
        if line.startswith("\x1f"):
            close()
            commit, path, in_hunk = line[1:], None, False
        elif line.startswith("diff --git "):
            close()
            path, in_hunk = None, False
        elif line.startswith("@@"):
            in_hunk = True
        elif not in_hunk and line.startswith("+++ "):
            target = line[4:]
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif in_hunk and path is not None and line.startswith("+"):
            added.append(line[1:])
    close()
    return sources


# --- 1. The assignment brief ----------------------------------------------------------------------


def brief_files(root: Path) -> list[Path]:
    """Every readable file under `BRIEF_SOURCES`, in a stable order."""
    found: list[Path] = []
    for name in BRIEF_SOURCES:
        source = root / name
        if source.is_dir():
            found += sorted(path for path in source.rglob("*") if path.is_file())
        elif source.is_file():
            found.append(source)
    return found


def phrases(text: str) -> Iterator[str]:
    """Every `PHRASE_WORDS`-word window of a text, taken every `PHRASE_STRIDE` words.

    Words rather than characters, lower-cased, punctuation dropped: the brief is prose, and a
    quotation of it that changed a comma or re-wrapped a line is the same leak.
    """
    words = [word.lower() for word in WORD.findall(text)]
    for start in range(0, len(words) - PHRASE_WORDS + 1, PHRASE_STRIDE):
        yield " ".join(words[start : start + PHRASE_WORDS])


def windows(text: str) -> set[str]:
    """Every `PHRASE_WORDS`-word window of a text, at a stride of one.

    The history is read at every offset even though the brief is sampled every three words: a
    quotation does not start where the sampling happened to.
    """
    words = [word.lower() for word in WORD.findall(text)]
    return {
        " ".join(words[start : start + PHRASE_WORDS])
        for start in range(len(words) - PHRASE_WORDS + 1)
    }


def check_brief(root: Path, sources: Iterable[Source]) -> Check:
    files = brief_files(root)
    if not files:
        return Check(
            1,
            "the assignment brief is not in the history",
            Status.UNRUN,
            f"none of {', '.join(BRIEF_SOURCES)} is present, so there was nothing to look for. "
            "A clone does not carry them — run this where the brief is.",
        )

    wanted = {phrase for path in files for phrase in phrases(_read(path))}
    named = ", ".join(str(path.relative_to(root)) for path in files)
    if not wanted:
        return Check(
            1,
            "the assignment brief is not in the history",
            Status.UNRUN,
            f"{named} yielded no phrase of {PHRASE_WORDS} words to look for.",
        )

    found: dict[str, list[str]] = {}
    for source in sources:
        for phrase in windows(source.text) & wanted:
            found.setdefault(phrase, []).append(source.where())

    scope = f"{len(wanted)} phrases of {PHRASE_WORDS} words from {named}"
    if not found:
        return Check(
            1,
            "the assignment brief is not in the history",
            Status.OK,
            f"{scope}; none of them appears in the history.",
        )
    return Check(
        1,
        "the assignment brief is not in the history",
        Status.FAIL,
        f"{scope}; {len(found)} of them appear in the history.",
        (
            *(f'"{phrase}" — {_places(places)}' for phrase, places in sorted(found.items())),
            "Judge each one. Six words of ordinary English can collide by chance; six words "
            "carrying the brief's own vocabulary, or several overlapping windows in one file, "
            "cannot. Only the matched words are shown — what surrounds them in the brief is not.",
        ),
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


PLACES_SHOWN: Final = 3


def _places(places: Sequence[str]) -> str:
    """Where a phrase was found, truncated — one occurrence is already enough to go and look."""
    unique = sorted(set(places))
    shown = ", ".join(unique[:PLACES_SHOWN])
    return shown + (f" (+{len(unique) - PLACES_SHOWN} more)" if len(unique) > PLACES_SHOWN else "")


# --- 2. Credentials -------------------------------------------------------------------------------


def check_credentials(sources: Iterable[Source]) -> Check:
    """Where every credential-shaped string in the history lives — never what it is.

    A match under `tests/` is a fixture the suite needs and is reported without failing the run; a
    match anywhere else is reported and does fail it. The distinction is the path, because that is
    the only thing about a match that can be stated without printing the match itself.
    """
    at: dict[str, set[str]] = {}
    total = 0
    for source in sources:
        for match in CREDENTIAL.finditer(source.text):
            at.setdefault(source.path, set()).add(match.group(1))
            total += 1

    if not at:
        return Check(2, "no credential-shaped string in the history", Status.OK, "no match.")

    leaked = sorted(path for path in at if not path.startswith(FIXTURE_ROOT))
    detail = tuple(
        f"{path}: {', '.join(f'{prefix}…' for prefix in sorted(at[path]))}"
        + ("" if path.startswith(FIXTURE_ROOT) else "   <- not a test fixture")
        for path in sorted(at)
    )
    prefixes = ", ".join(f"{prefix}…" for prefix in CREDENTIAL_PREFIXES)
    if not leaked:
        return Check(
            2,
            "no credential-shaped string in the history",
            Status.OK,
            f"{total} matches of {prefixes} in {len(at)} files, all of them under {FIXTURE_ROOT} "
            "and therefore test fixtures. Only the prefix and the path are shown.",
            detail,
        )
    return Check(
        2,
        "no credential-shaped string in the history",
        Status.FAIL,
        f"{total} matches of {prefixes} in {len(at)} files, {len(leaked)} of them outside "
        f"{FIXTURE_ROOT}. Only the prefix and the path are shown — read the files named below "
        "yourself, and rotate anything real.",
        detail,
    )


# --- 3 and 4. The git-ignored files ---------------------------------------------------------------


def ignored(path: str) -> bool:
    return any(
        path.startswith(spec) if spec.endswith("/") else path == spec for spec in IGNORED_PATHS
    )


def check_never_added(root: Path) -> Check:
    """`--diff-filter=A` over every branch: a file deleted again is still in the history."""
    stream = git(root, "log", "--all", "--diff-filter=A", "--name-only", "--format=%x1f%H")
    commit = ""
    added: dict[str, set[str]] = {}
    for line in stream.splitlines():
        if line.startswith("\x1f"):
            commit = line[1:]
        elif line.strip() and ignored(line):
            added.setdefault(line, set()).add(commit[:8])

    listed = ", ".join(IGNORED_PATHS)
    if not added:
        return Check(
            3,
            "the git-ignored files were never committed",
            Status.OK,
            f"no commit on any branch ever added {listed}.",
        )
    return Check(
        3,
        "the git-ignored files were never committed",
        Status.FAIL,
        f"{len(added)} such files were added at some point. Removing them now does not help — the "
        "commit that added them is what a clone carries.",
        tuple(
            f"{path}: added in {', '.join(sorted(commits))}"
            for path, commits in sorted(added.items())
        ),
    )


def check_still_ignored(root: Path) -> Check:
    """That `.gitignore` still covers them, so that check 3 keeps being true tomorrow."""
    unignored = [
        path
        for path in IGNORED_PATHS
        if subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--no-index", path.rstrip("/")],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    ]
    listed = ", ".join(IGNORED_PATHS)
    if not unignored:
        return Check(4, "they are still git-ignored", Status.OK, f"{listed} are all ignored.")
    return Check(
        4,
        "they are still git-ignored",
        Status.FAIL,
        f"{len(unignored)} of {len(IGNORED_PATHS)} are no longer ignored, so nothing stops the "
        "next commit from carrying them.",
        tuple(f"{path}: not ignored" for path in unignored),
    )


# --- The run --------------------------------------------------------------------------------------


def audit(root: Path) -> list[Check]:
    sources = history(root)
    return [
        check_brief(root, sources),
        check_credentials(sources),
        check_never_added(root),
        check_still_ignored(root),
    ]


def report(root: Path, checks: Sequence[Check]) -> None:
    print(f"audit of {root} — is this repository safe to make public?\n")
    for check in checks:
        print(f"{check.status.value:>10}  {check.number}. {check.title}")
        print(f"            {check.summary}")
        for line in check.detail:
            print(f"              - {line}")
        print()

    unrun = [check for check in checks if check.status is Status.UNRUN]
    failed = [check for check in checks if check.status is Status.FAIL]
    if not unrun and not failed:
        print("Clean. Nothing in the history argues against publishing.")
        return
    for check in unrun:
        print(f"COULD NOT RUN: check {check.number}, {check.title}.")
    for check in failed:
        print(f"FAILED: check {check.number}, {check.title}.")
    print(
        "\nThe repository was not made public — this script never does that, and does not read "
        "as a verdict. Resolve or judge each line above first."
    )


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the whole git history for the assignment brief, for credentials, and "
        "for the git-ignored files, before the repository is made public. Read-only: it changes "
        "nothing, here or on GitHub."
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        metavar="PATH",
        help="A directory inside the repository to audit (default: the current one).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        root = repository_root(args.repo)
        checks = audit(root)
    except AuditError as exc:
        print(f"the audit could not run: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    report(root, checks)
    if any(check.status is Status.UNRUN for check in checks):
        return EXIT_CANNOT_RUN
    if any(check.status is Status.FAIL for check in checks):
        return EXIT_FINDINGS
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
