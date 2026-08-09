"""`scripts/audit_history.py` is the gate in front of making this repository public, so what it has
to be trusted for is the *negative* answer: that a clean run means the brief and the credentials are
genuinely not in the history, and not that the script looked in the wrong place.

Every test therefore builds a throwaway repository with something planted in it and asserts the
script finds that thing — a secret in a file, a secret in a file that was deleted again, a secret in
a merge resolution that exists in neither parent, a quotation of the brief in a document and in a
commit message. The script is driven as a subprocess from that repository's root, the way an
operator runs it.

Two properties get their own assertions because they are the point of the script rather than a
detail of it: the brief sentence that was planted never appears in the output, and a repository with
no brief present fails loudly rather than reporting that check 1 found nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_history.py"

BRIEF_SENTENCE = (
    "The candidate must deliver an orchestrator that turns a labelled issue into a merged pull "
    "request, and the write-up matters as much as the code does."
)
"""Stands in for a sentence of the assignment brief. Invented, and long enough that a six-word
window of it is not something ordinary prose produces by accident."""

QUOTATION = "turns a labelled issue into a merged pull request"
"""What a document leaks when somebody pastes the brief's own wording into it."""

SECRET = "cog_live_9f3a1c7d2b4e6f8a0c5d"
"""Shaped like a Devin token, which is all the check looks at."""


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit(repo: Path, path: str, text: str, message: str = "Add a file") -> None:
    write(repo, path, text)
    git(repo, "add", "--", path)
    git(repo, "commit", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository shaped like this one: the brief present but ignored, and nothing else."""
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "audit@example.invalid")
    git(tmp_path, "config", "user.name", "Audit")
    write(tmp_path, "requirements/instructions.md", BRIEF_SENTENCE)
    write(tmp_path, "CLAUDE.local.md", "Personal notes.\n")
    write(tmp_path, ".env", "DEVIN_API_TOKEN=\n")
    commit(tmp_path, ".gitignore", "requirements/\nCLAUDE.local.md\n.env\n", "Ignore the brief")
    commit(tmp_path, "README.md", "# A project\n\nIt does a thing.\n")
    return tmp_path


def audit(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "--script", str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def finding(repo: Path) -> str:
    """Run the audit expecting it to object, and return what it left the operator."""
    result = audit(repo)
    assert result.returncode == 1, f"expected a finding, got {result.returncode}:\n{result.stdout}"
    assert "Traceback" not in result.stderr, result.stderr
    return result.stdout


def test_a_clean_repository_passes(repo: Path) -> None:
    result = audit(repo)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "Clean." in result.stdout
    assert result.stdout.count("        OK") == 4


def test_a_credential_in_the_history_is_found_but_never_printed(repo: Path) -> None:
    commit(repo, "src/settings.py", f'TOKEN = "{SECRET}"\n')

    report = finding(repo)

    assert "src/settings.py" in report
    assert "not a test fixture" in report
    assert "FAILED: check 2" in report
    assert SECRET not in report, "the audit printed the credential it was reporting"


def test_a_credential_under_tests_is_named_as_a_fixture_and_does_not_fail(repo: Path) -> None:
    commit(repo, "tests/test_redaction.py", f'TOKEN = "{SECRET}"\n')

    result = audit(repo)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "tests/test_redaction.py" in result.stdout
    assert "not a test fixture" not in result.stdout
    assert SECRET not in result.stdout


def test_a_credential_deleted_again_is_still_in_the_history(repo: Path) -> None:
    commit(repo, "src/settings.py", f'TOKEN = "{SECRET}"\n')
    git(repo, "rm", "--", "src/settings.py")
    git(repo, "commit", "-m", "Remove the file")

    report = finding(repo)

    assert "src/settings.py" in report
    assert SECRET not in report


def test_a_credential_introduced_by_a_merge_resolution_is_found(repo: Path) -> None:
    """Content that exists in a merge commit and in neither of its parents.

    `git log -p` shows no diff at all for a merge unless it is asked to, so this is the case a
    history scan silently steps over — and a hand-resolved conflict is exactly where a stray line
    gets in, because nobody reviewed the resolution as a diff.
    """
    git(repo, "checkout", "-b", "side")
    commit(repo, "config.py", "TOKEN = None\n")
    git(repo, "checkout", "main")
    commit(repo, "config.py", "TOKEN = ''\n")
    subprocess.run(
        ["git", "-C", str(repo), "merge", "side"], check=False, capture_output=True, text=True
    )
    write(repo, "config.py", f'TOKEN = "{SECRET}"\n')
    git(repo, "add", "--", "config.py")
    git(repo, "commit", "--no-edit")

    report = finding(repo)

    assert "config.py" in report
    assert "FAILED: check 2" in report
    assert SECRET not in report


def test_the_brief_quoted_in_a_document_is_found_and_the_sentence_is_not_printed(
    repo: Path,
) -> None:
    commit(repo, "docs/design.md", f"# Design\n\nThe goal is a system that {QUOTATION}.\n")

    report = finding(repo)

    assert "FAILED: check 1" in report
    assert "docs/design.md" in report
    assert "that turns a labelled issue into" in report, "the matched words are not shown"
    assert BRIEF_SENTENCE not in report, "the audit printed the brief it was checking for"


def test_the_brief_quoted_in_a_commit_message_is_found(repo: Path) -> None:
    commit(repo, "src/app.py", "def main() -> None: ...\n", f"Build the part that {QUOTATION}")

    report = finding(repo)

    assert "FAILED: check 1" in report
    assert "(commit message)" in report
    assert BRIEF_SENTENCE not in report


def test_a_brief_that_is_not_there_fails_loudly_instead_of_finding_nothing(repo: Path) -> None:
    (repo / "requirements" / "instructions.md").unlink()
    (repo / "requirements").rmdir()
    (repo / "CLAUDE.local.md").unlink()

    result = audit(repo)

    assert result.returncode == 2, f"{result.stdout}\n{result.stderr}"
    assert "CANNOT RUN" in result.stdout
    assert "COULD NOT RUN: check 1" in result.stdout
    assert "nothing to look for" in result.stdout


def test_committing_an_ignored_file_is_found_even_though_it_is_ignored(repo: Path) -> None:
    git(repo, "add", "--force", "--", "requirements/instructions.md")
    git(repo, "commit", "-m", "Add the brief by mistake")

    report = finding(repo)

    assert "FAILED: check 3" in report
    assert "requirements/instructions.md" in report
    assert BRIEF_SENTENCE not in report


def test_un_ignoring_the_brief_is_found(repo: Path) -> None:
    commit(repo, ".gitignore", ".env\n", "Trim the ignore list")

    report = finding(repo)

    assert "FAILED: check 4" in report
    assert "requirements/: not ignored" in report
    assert "CLAUDE.local.md: not ignored" in report


def state(repo: Path) -> str:
    """Everything a run of the audit must leave exactly as it found it."""
    return (
        subprocess.run(
            ["git", "-C", str(repo), "log", "--all", "--format=%H", "--name-status"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        + subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


def test_it_is_read_only(repo: Path) -> None:
    before = state(repo)

    audit(repo)

    assert state(repo) == before
