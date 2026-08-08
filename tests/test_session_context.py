"""The two hooks in `.claude/hooks/` are the only part of this workflow that is enforced rather
than merely written down, and nothing exercised them.

`session-init.sh` runs at the start of every session, so how it *fails* matters more than what it
reports: a hook that errors, hangs or emits malformed JSON degrades every new session in the
repository. Every assertion below therefore checks that a broken environment still yields exit 0
and no output, rather than a traceback.

`guard-pr-create.sh` is the enforcement behind rule 3 in `CLAUDE.md`. The marker file it checks is
the only thing between an unreviewed branch and an open pull request, so the tests pin the four
decisions it can make.

Both are run as subprocesses against a throwaway git repository with `gh` stubbed out, which is how
Claude Code invokes them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSION_CONTEXT = ROOT / "scripts" / "session_context.py"
SESSION_INIT = ROOT / ".claude" / "hooks" / "session-init.sh"
GUARD = ROOT / ".claude" / "hooks" / "guard-pr-create.sh"

UV = shutil.which("uv") or "uv"

# Holds git and python3 but neither `gh` nor `uv`. Absent tools are the case worth covering: an exec
# that raises OSError takes a different path through the scripts than one that exits non-zero.
MINIMAL_PATH = os.pathsep.join(["/usr/bin", "/bin"])

# The tests create branches and commits; an ambient global gitconfig (hooks, signing, templates)
# would otherwise decide whether that works.
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

TASKS_YAML = """\
areas: [ops, pipeline, dashboard]
tasks:
  - id: T05
    title: Test harness
    wave: 0
    area: ops
  - id: T06
    title: Claude Code collaboration setup
    wave: 0
    area: ops
    spec: docs/implementation-plan.md#parallel-sessions
    adrs: [2026-08-07-enforce-workflow-with-hooks]
    owns:
      - CLAUDE.md
      - .claude/hooks/
    notes: >-
      SessionStart hook injects live task state;
      PreToolUse hook blocks `gh pr create`.
  - id: T14
    title: State machine
    wave: 2
    area: pipeline
    depends_on: [T05, T06]
  - id: T30
    title: Dashboard shell
    wave: 3
    area: dashboard
    ui: true
  - id: T42
    title: Never seeded as an issue
    wave: 1
    area: ops
"""


def git(project: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        env={**os.environ, **GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A repository on a task branch, holding nothing but what the hooks read."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "tasks.yaml").write_text(TASKS_YAML)
    git(tmp_path, "init", "-q", "-b", "task/T06-setup")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Sentinel test")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "root")
    return tmp_path


def issue(
    task: str, number: int, title: str = "Some task", state: str = "OPEN", assigned: bool = False
) -> dict:
    return {
        "number": number,
        "title": f"{task}: {title}",
        "state": state,
        "assignees": [{"login": "someone"}] if assigned else [],
    }


def stub_gh(project: Path, script: str) -> Path:
    """Write a fake `gh` (the body of a /bin/sh script) and return the directory to put on PATH."""
    stubs = project / "stubs"
    stubs.mkdir(exist_ok=True)
    fake = stubs / "gh"
    fake.write_text(f"#!/bin/sh\n{script}\n")
    fake.chmod(0o755)
    return stubs


def stub_gh_listing(project: Path, *issues: dict) -> Path:
    return stub_gh(project, f"cat <<'JSON'\n{json.dumps(list(issues))}\nJSON")


def path_with(stubs: Path, base: str = MINIMAL_PATH) -> str:
    return os.pathsep.join([str(stubs), base])


def briefing(project: Path, path: str = MINIMAL_PATH) -> str:
    """Run the briefing script the way the hook does, insisting it never crashes."""
    result = subprocess.run(
        [UV, "run", "--script", str(SESSION_CONTEXT)],
        cwd=project,
        env={**os.environ, "PATH": path, **GIT_ENV},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    return result.stdout


# --------------------------------------------------------------------------- the briefing


def test_a_task_branch_is_briefed_on_its_own_task(project: Path) -> None:
    path = path_with(stub_gh_listing(project, issue("T06", 6)))

    out = briefing(project, path)

    assert "## Your task: T06 — Claude Code collaboration setup" in out
    assert "Issue: #6 (open)" in out
    assert "SessionStart hook injects live task state" in out
    assert "**Spec:** `docs/implementation-plan.md#parallel-sessions`" in out
    assert "- `docs/adr/2026-08-07-enforce-workflow-with-hooks.md`" in out
    # Rule 5 is the whole collision-avoidance strategy, so the file list has to reach the session
    # that is about to edit files.
    assert "**Owned files — do not modify anything outside this list:**" in out
    assert "- CLAUDE.md" in out
    assert "- .claude/hooks/" in out
    # Someone else's task must not be advertised to a session that already has one.
    assert "Ready to start" not in out


def test_a_ui_task_is_told_that_component_tests_are_mandatory(project: Path) -> None:
    git(project, "checkout", "-q", "-b", "task/T30-dashboard")
    path = path_with(stub_gh_listing(project, issue("T30", 30)))

    out = briefing(project, path)

    assert "## Your task: T30 — Dashboard shell" in out
    assert "component tests are mandatory" in out
    assert "browser-level end-to-end test" in out


def test_a_branch_that_is_not_a_task_branch_is_offered_what_is_free(project: Path) -> None:
    git(project, "checkout", "-q", "-b", "main")
    path = path_with(
        stub_gh_listing(
            project,
            issue("T05", 5, state="CLOSED"),
            issue("T06", 6),
            issue("T14", 14),
            issue("T30", 30, assigned=True),
        )
    )

    out = briefing(project, path)

    assert "Branch `main` is not a task branch." in out
    # T05 is done, T14 waits on T06, T30 is claimed, T42 was never seeded as an issue.
    assert "## Ready to start (1 unblocked, 1 waiting on dependencies)" in out
    assert "| 0 | T06 — Claude Code collaboration setup | #6 |" in out
    for absent in ("T05", "T14", "T30", "T42"):
        assert absent not in out
    assert "git worktree add" in out


def test_a_branch_naming_a_task_that_does_not_exist_falls_back_to_the_ready_list(
    project: Path,
) -> None:
    # A typo in the branch name must not silently brief the session on nothing at all.
    git(project, "checkout", "-q", "-b", "task/T99-invented")
    path = path_with(stub_gh_listing(project, issue("T06", 6)))

    out = briefing(project, path)

    assert "Branch `task/T99-invented` is not a task branch." in out
    assert "## Ready to start" in out


def test_everything_claimed_or_blocked_reports_that_rather_than_an_empty_table(
    project: Path,
) -> None:
    git(project, "checkout", "-q", "-b", "main")
    path = path_with(stub_gh_listing(project, issue("T06", 6, assigned=True), issue("T14", 14)))

    out = briefing(project, path)

    assert "## No unblocked tasks" in out
    assert "1 task(s) are waiting on dependencies" in out


def test_open_blockers_are_named_before_an_external_dependency_is_assumed(project: Path) -> None:
    (project / "docs" / "blockers.md").write_text(
        "| Open | B8 | Devin credentials |\n"
        "| Closed | B1 | Fork created |\n"
        "| Open | B9 | Webhook |\n"
    )
    path = path_with(stub_gh_listing(project, issue("T06", 6)))

    assert "**2 open blockers**" in briefing(project, path)


@pytest.mark.parametrize(
    "gh_script",
    [
        "exit 1",
        "exit 0",
        "echo 'gh: not logged in'; exit 4",
        "printf 'not json at all'",
        'printf \'[{"number": 6, "tit\'',
    ],
    ids=["failure", "silence", "unauthenticated", "not-json", "truncated-json"],
)
def test_gh_returning_anything_unusable_degrades_instead_of_crashing(
    project: Path, gh_script: str
) -> None:
    # This runs before every session in every worktree. `gh` not being logged in, rate-limited or
    # offline is ordinary, and the briefing still has to say which branch this is and what to do.
    git(project, "checkout", "-q", "-b", "main")
    path = path_with(stub_gh(project, gh_script))

    out = briefing(project, path)

    assert "## Task state unavailable" in out
    assert "gh issue list -R taxpon/sentinel --label task --state open" in out


def test_gh_being_absent_entirely_degrades_the_same_way(project: Path) -> None:
    git(project, "checkout", "-q", "-b", "main")

    out = briefing(project)  # MINIMAL_PATH: no `gh` to exec at all

    assert "## Task state unavailable" in out


def test_a_task_branch_is_still_briefed_without_github(project: Path) -> None:
    # The task graph is in the repository; only the issue number needs the network.
    out = briefing(project)

    assert "## Your task: T06 — Claude Code collaboration setup" in out
    assert "Issue: #" not in out


def test_outside_a_checkout_of_this_project_there_is_no_briefing(project: Path) -> None:
    (project / "docs" / "tasks.yaml").unlink()

    assert briefing(project) == ""


# --------------------------------------------------------------------------- the SessionStart hook


def run_session_init(project: Path, path: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(SESSION_INIT)],
        cwd=project,
        env={**os.environ, "PATH": path, "CLAUDE_PROJECT_DIR": str(project), **GIT_ENV},
        capture_output=True,
        text=True,
    )
    # A SessionStart hook that exits non-zero puts an error in front of the human on every single
    # session start. Silence is the only acceptable failure.
    assert result.returncode == 0, result.stderr
    return result


def install_briefing_script(project: Path, source: str | None = None) -> None:
    (project / "scripts").mkdir(exist_ok=True)
    target = project / "scripts" / "session_context.py"
    if source is None:
        shutil.copy(SESSION_CONTEXT, target)
    else:
        target.write_text(source)


def test_the_hook_hands_the_briefing_to_the_session_as_additional_context(project: Path) -> None:
    install_briefing_script(project)
    path = path_with(stub_gh_listing(project, issue("T06", 6)), os.environ["PATH"])

    result = run_session_init(project, path)

    payload = json.loads(result.stdout)["hookSpecificOutput"]
    assert payload["hookEventName"] == "SessionStart"
    assert "## Your task: T06 — Claude Code collaboration setup" in payload["additionalContext"]


def test_a_briefing_full_of_json_metacharacters_survives_the_envelope(project: Path) -> None:
    # The briefing is interpolated into JSON by the hook; ADR titles and task notes are free text.
    awkward = 'He said "no" \\ and\nthen — a tab\there'
    install_briefing_script(project, f"print({awkward!r})")
    path = path_with(stub_gh(project, "exit 1"), os.environ["PATH"])

    result = run_session_init(project, path)

    assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == awkward + "\n"


@pytest.mark.parametrize(
    "source",
    [None, "import sys; sys.exit(1)", "raise RuntimeError('boom')", "pass", "print('')"],
    ids=["script-absent", "exits-nonzero", "raises", "prints-nothing", "prints-blank"],
)
def test_a_briefing_that_cannot_be_produced_leaves_the_session_alone(
    project: Path, source: str | None
) -> None:
    if source is not None:
        install_briefing_script(project, source)
    path = path_with(stub_gh(project, "exit 1"), os.environ["PATH"])

    result = run_session_init(project, path)

    assert result.stdout == ""


def test_without_uv_the_hook_says_nothing_rather_than_failing(project: Path) -> None:
    install_briefing_script(project)

    result = run_session_init(project, MINIMAL_PATH)

    assert result.stdout == ""


# --------------------------------------------------------------------------- the pre-PR guard


def run_guard(project: Path, command: str, *, project_dir: bool = True) -> dict | None:
    """Run the guard over a Bash command; return its decision, or None if it kept out of the way."""
    env = {**os.environ, **GIT_ENV}
    if project_dir:
        env["CLAUDE_PROJECT_DIR"] = str(project)
    else:
        env.pop("CLAUDE_PROJECT_DIR", None)
    result = subprocess.run(
        [str(GUARD)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    # The guard sits in front of *every* Bash call. Crashing it would break the tool entirely, so a
    # failure to decide must still be exit 0 with no opinion.
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)["hookSpecificOutput"]


def record_review(project: Path, sha: str, branch: str = "task/T06-setup") -> None:
    """Write the marker exactly as step 5 of `/finish-task` does, trailing newline and all."""
    markers = project / ".sentinel-review"
    markers.mkdir(exist_ok=True)
    (markers / f"{branch.replace('/', '_')}.ok").write_text(f"{sha}\n")


def denial(decision: dict | None) -> str:
    assert decision is not None, "the guard allowed a pull request it should have blocked"
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    return decision["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "gh pr list",
        "gh pr view 12 --json state",
        "gh issue create --title 'T06: something' --body x",
        "make test",
    ],
    ids=["git", "pr-list", "pr-view", "issue-create", "make"],
)
def test_commands_that_are_not_opening_a_pull_request_pass_through(
    project: Path, command: str
) -> None:
    assert run_guard(project, command) is None


def test_opening_a_pull_request_with_no_recorded_review_is_denied(project: Path) -> None:
    reason = denial(run_guard(project, "gh pr create --base main --title 'T06: x' --body y"))

    assert "task/T06-setup" in reason
    assert "/finish-task" in reason
    # The message has to say what to do, or the next session invents a way around it.
    assert "pr-review-toolkit" in reason


def test_a_review_recorded_against_head_opens_the_gate(project: Path) -> None:
    record_review(project, git(project, "rev-parse", "HEAD"))

    assert run_guard(project, "gh pr create --fill") is None


def test_a_review_recorded_before_the_last_commit_is_rejected_as_stale(project: Path) -> None:
    reviewed = git(project, "rev-parse", "HEAD")
    record_review(project, reviewed)
    git(project, "commit", "-q", "--allow-empty", "-m", "one more thing, unreviewed")

    reason = denial(run_guard(project, "gh pr create --fill"))

    assert "stale" in reason
    assert reviewed[:12] in reason
    assert git(project, "rev-parse", "HEAD")[:12] in reason


def test_a_review_of_a_different_branch_does_not_open_the_gate(project: Path) -> None:
    # Both branches share a HEAD sha here, so only the branch in the filename can distinguish them.
    record_review(project, git(project, "rev-parse", "HEAD"), branch="task/T05-harness")

    assert "/finish-task" in denial(run_guard(project, "gh pr create --fill"))


@pytest.mark.parametrize(
    "command",
    [
        "printf '%s' \"$BODY\" | gh pr create --base main --body-file -",
        "git push -u origin HEAD && gh pr create --fill",
        "GH_TOKEN=$TOKEN gh pr create --fill",
        "gh -R taxpon/sentinel pr create --fill",
        "cd /tmp/x; gh pr create --draft",
    ],
    ids=["pipeline", "chained", "env-prefix", "flag-before-pr", "after-cd"],
)
def test_gh_pr_create_is_caught_wherever_it_sits_in_the_command(
    project: Path, command: str
) -> None:
    assert denial(run_guard(project, command))


def test_the_matcher_would_rather_deny_too_much_than_too_little(project: Path) -> None:
    # `gh pr create` inside a quoted string is denied too. That is the safe direction to be wrong
    # in: the human sees the reason and rephrases, whereas the opposite error opens an unreviewed
    # pull request silently. Do not "fix" this by narrowing the match without replacing it with
    # something that still catches the forms above.
    assert denial(run_guard(project, "git commit -m 'ready to gh pr create now'"))


def test_a_detached_head_cannot_be_reviewed_so_it_is_denied(project: Path) -> None:
    git(project, "checkout", "-q", "--detach")

    assert "cannot verify" in denial(run_guard(project, "gh pr create --fill"))


def test_the_guard_finds_the_repository_without_being_told_where_it_is(project: Path) -> None:
    # CLAUDE_PROJECT_DIR is set by Claude Code, but a session started from a subdirectory or a
    # different launcher must not silently lose the guard.
    record_review(project, git(project, "rev-parse", "HEAD"))

    assert run_guard(project, "gh pr create --fill", project_dir=False) is None
    (project / ".sentinel-review").rename(project / ".sentinel-review-moved")
    assert denial(run_guard(project, "gh pr create --fill", project_dir=False))
