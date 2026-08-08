"""`docs/adr/index.md` and `.claude/rules/adr-pointers.md` are generated from ADR front matter by
`scripts/gen_adr_index.py`, and CI trusts that output (`make adr-check`). Every session writes
records into the same directory, so the generator's real job is to reject a bad record loudly
instead of folding it into a plausible-looking index.

The generator is exercised as a subprocess against a throwaway repository, which is how the Makefile
and CI invoke it, and the assertions are on the exit status and the message a human is left with.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gen_adr_index.py"
ARCHITECTURE = ROOT / "docs" / "02-architecture.md"
ADR_DIR = ROOT / "docs" / "adr"

TASKS_YAML = """\
areas:
  - ops
  - pipeline
tasks:
  - id: T07
  - id: T14
"""

VALID_FIELDS = {
    "title": "Do the thing this way",
    "status": "accepted",
    "date": "2026-08-01",
    "type": "architecture",
    "areas": "[ops]",
    "tasks": "[T07]",
    "files": "[src/sentinel/thing.py]",
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A repository root holding nothing but what the generator reads."""
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "tasks.yaml").write_text(TASKS_YAML)
    return tmp_path


def front_matter(**overrides: str | None) -> str:
    """Valid front matter with fields replaced, or dropped when the override is None."""
    fields = {**VALID_FIELDS, **overrides}
    return "".join(f"{key}: {value}\n" for key, value in fields.items() if value is not None)


def write_record(project: Path, slug: str, **overrides: str | None) -> Path:
    return write_raw(project, slug, f"---\n{front_matter(**overrides)}---\n\n# {slug}\n")


def write_raw(project: Path, slug: str, text: str) -> Path:
    path = project / "docs" / "adr" / f"{slug}.md"
    path.write_text(text)
    return path


def generate(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "--script", str(SCRIPT), *args],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )


def rejected(project: Path) -> str:
    """Run the generator expecting a refusal, and return the message it left behind."""
    result = generate(project)
    assert result.returncode == 1, f"expected a refusal, got:\n{result.stdout}{result.stderr}"
    assert "Traceback" not in result.stderr, f"crashed instead of reporting:\n{result.stderr}"
    return result.stderr


def index_of(project: Path) -> str:
    return (project / "docs" / "adr" / "index.md").read_text()


def test_a_record_reaches_the_index_and_the_pointer_rule(project: Path) -> None:
    write_record(project, "2026-08-01-do-the-thing")

    result = generate(project)

    assert result.returncode == 0, result.stderr
    index = index_of(project)
    assert "| 2026-08-01 | [Do the thing this way](./2026-08-01-do-the-thing.md)" in index
    assert "- **ops** — [Do the thing this way](./2026-08-01-do-the-thing.md)" in index
    assert "| T07 | [Do the thing this way](./2026-08-01-do-the-thing.md) |" in index

    pointers = (project / ".claude" / "rules" / "adr-pointers.md").read_text()
    assert '  - "src/sentinel/thing.py"' in pointers
    assert "`src/sentinel/thing.py` — [Do the thing this way]" in pointers


def test_regenerating_changes_nothing_and_check_agrees(project: Path) -> None:
    write_record(project, "2026-08-01-do-the-thing")
    write_record(project, "2026-08-02-do-another-thing", title="Another", date="2026-08-02")

    generate(project)
    first = index_of(project)
    generate(project)

    assert index_of(project) == first
    assert generate(project, "--check").returncode == 0


def test_check_fails_on_an_index_that_no_longer_matches_the_records(project: Path) -> None:
    write_record(project, "2026-08-01-do-the-thing")
    generate(project)
    write_record(project, "2026-08-01-do-the-thing", title="A different decision entirely")

    result = generate(project, "--check")

    assert result.returncode == 1
    assert "docs/adr/index.md" in result.stderr
    assert "make adr-index" in result.stderr


def test_a_broken_record_leaves_the_previous_index_untouched(project: Path) -> None:
    write_record(project, "2026-08-01-do-the-thing")
    generate(project)
    good = index_of(project)
    write_record(project, "2026-08-02-broken", date="2026-08-02", areas="[not-an-area]")

    assert "nothing written" in rejected(project)
    # A partially generated index is worse than a stale one: `make adr-check` would then pass on
    # output that silently dropped a record.
    assert index_of(project) == good


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# No front matter\n", "missing YAML front matter"),
        ("---\ntitle: Half a record\n", "never closed by a second ---"),
        ("---\n- a list, not fields\n---\n", "expected a mapping of fields"),
        ("---\ntitle: Bad\n  status: accepted\n---\n", "not valid YAML"),
    ],
    ids=["absent", "unterminated", "not-a-mapping", "malformed"],
)
def test_front_matter_that_cannot_be_read_names_the_file(
    project: Path, text: str, expected: str
) -> None:
    write_raw(project, "2026-08-01-broken", text)

    message = rejected(project)

    assert "docs/adr/2026-08-01-broken.md" in message
    assert expected in message


def test_a_missing_required_field_is_named(project: Path) -> None:
    write_record(project, "2026-08-01-do-the-thing", status=None, areas="[]")

    message = rejected(project)

    assert "missing or empty: status, areas" in message


def test_an_area_outside_the_vocabulary_is_rejected_with_the_vocabulary(project: Path) -> None:
    write_record(project, "2026-08-01-do-the-thing", areas="[piepline]")

    message = rejected(project)

    assert "area 'piepline' is not one of the areas in docs/tasks.yaml: ops, pipeline" in message


def test_a_task_that_does_not_exist_is_rejected(project: Path) -> None:
    write_record(project, "2026-08-01-do-the-thing", tasks="[T07, T99]")

    assert "task 'T99' is not a task in docs/tasks.yaml" in rejected(project)


def test_a_mistyped_field_name_is_rejected_rather_than_ignored(project: Path) -> None:
    # `area:` instead of `areas:` used to leave the record out of the by-area table with no warning.
    write_record(project, "2026-08-01-do-the-thing", area="ops")

    message = rejected(project)

    assert "unknown front matter field(s) area" in message


def test_a_scalar_where_a_list_belongs_is_rejected(project: Path) -> None:
    # `areas: ops` iterates as three one-character areas, which the index would happily render.
    write_record(project, "2026-08-01-do-the-thing", areas="ops")

    assert "areas must be a list of strings, got 'ops'" in rejected(project)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"status": "acccepted"}, "status 'acccepted' is not one of accepted"),
        ({"type": "architectural"}, "type 'architectural' is not one of architecture, process"),
        ({"date": "yesterday"}, "date 'yesterday' is not a YYYY-MM-DD date"),
        ({"date": "2026-08-09"}, "date 2026-08-09 disagrees with the filename"),
    ],
    ids=["status", "type", "unparseable-date", "date-vs-filename"],
)
def test_values_outside_the_documented_vocabulary_are_rejected(
    project: Path, overrides: dict[str, str], expected: str
) -> None:
    # The date is what the by-date table sorts on, so a string that is not a date sorts by accident.
    write_record(project, "2026-08-01-do-the-thing", **overrides)

    assert expected in rejected(project)


def test_supersedes_must_name_a_record_that_exists(project: Path) -> None:
    write_record(project, "2026-08-01-do-the-thing", supersedes="2025-01-01-never-written")

    message = rejected(project)

    assert "supersedes '2025-01-01-never-written', and no record with that slug exists" in message


def test_superseding_a_record_that_still_claims_to_be_accepted_is_rejected(project: Path) -> None:
    write_record(project, "2026-08-01-the-old-way")
    write_record(
        project,
        "2026-08-02-the-new-way",
        title="The new way",
        date="2026-08-02",
        supersedes="2026-08-01-the-old-way",
    )

    message = rejected(project)

    assert "whose status is still 'accepted'" in message


def test_a_supersession_is_visible_wherever_the_old_record_is_listed(project: Path) -> None:
    write_record(project, "2026-08-01-the-old-way", title="The old way", status="superseded")
    write_record(
        project,
        "2026-08-02-the-new-way",
        title="The new way",
        date="2026-08-02",
        supersedes="2026-08-01-the-old-way",
    )

    assert generate(project).returncode == 0

    index = index_of(project)
    assert "| superseded → [The new way](./2026-08-02-the-new-way.md) |" in index
    assert "[The old way](./2026-08-01-the-old-way.md) (superseded)" in index


def test_two_records_cannot_supersede_the_same_one(project: Path) -> None:
    write_record(project, "2026-08-01-the-old-way", status="superseded")
    for slug in ("2026-08-02-one-way", "2026-08-03-another-way"):
        write_record(project, slug, title=slug, date=slug[:10], supersedes="2026-08-01-the-old-way")

    assert "which 2026-08-02-one-way also supersedes" in rejected(project)


def test_every_problem_in_the_directory_is_reported_by_one_run(project: Path) -> None:
    write_record(project, "2026-08-01-do-the-thing", areas="[nonsense]")
    write_record(project, "2026-08-02-do-another-thing", date="2026-08-02", tasks="[T99]")

    message = rejected(project)

    assert "2026-08-01-do-the-thing.md" in message
    assert "2026-08-02-do-another-thing.md" in message
    assert "2 problem(s)" in message


def test_running_outside_the_repository_root_refuses_to_write_an_empty_index(
    tmp_path: Path,
) -> None:
    # The glob would otherwise match nothing and overwrite the index with a record-free table.
    result = generate(tmp_path)

    assert result.returncode == 1
    assert "run this from the repository root" in result.stderr


def test_an_empty_adr_directory_is_an_error_not_an_empty_index(project: Path) -> None:
    result = generate(project)

    assert result.returncode == 1
    assert "no decision records found" in result.stderr


def design_decisions_links() -> list[str]:
    """The ADR filenames linked from the Design decisions table in docs/02-architecture.md."""
    section = ARCHITECTURE.read_text().split("## Design decisions", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"\]\(\./adr/([^)]+)\)", section)


def architecture_records() -> list[str]:
    """Every record whose front matter says `type: architecture`."""
    return sorted(
        path.name
        for path in ADR_DIR.glob("*.md")
        if path.name not in {"index.md", "template.md"}
        and re.search(r"^type: architecture\s*$", path.read_text(), re.MULTILINE)
    )


def test_the_design_decisions_table_is_where_we_think_it_is() -> None:
    # Without this, restructuring docs/02-architecture.md would empty the table and the two tests
    # below would pass vacuously.
    assert len(design_decisions_links()) >= 7
    assert len(architecture_records()) >= 7


def test_every_design_decision_links_to_a_record_that_exists() -> None:
    broken = sorted(name for name in design_decisions_links() if not (ADR_DIR / name).is_file())
    assert not broken, f"linked from {ARCHITECTURE.name} but absent from docs/adr/: {broken}"


def test_every_architecture_record_appears_in_the_design_decisions_table() -> None:
    # The table went stale within days of being written because nothing checked it. A process
    # record belongs in docs/adr/ alone; an architecture one is part of the architecture.
    linked = set(design_decisions_links())
    missing = [name for name in architecture_records() if name not in linked]
    assert not missing, (
        f"type: architecture records missing a row in {ARCHITECTURE.name}: {missing}"
    )
