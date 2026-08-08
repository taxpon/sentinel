"""`.env.example` and the configuration table in docs/09-operations.md are two hand-written lists of
the same thing. These tests fail the moment they disagree, so neither can quietly go stale."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "09-operations.md"
ENV_EXAMPLE = ROOT / ".env.example"

ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


@dataclass(frozen=True)
class Documented:
    required: bool
    default: str


def _cell(text: str) -> str:
    return text.strip().strip("`").strip()


def documented_variables() -> dict[str, Documented]:
    """The `## Configuration` table, as {name: (required, default)}."""
    section: list[str] = []
    in_section = False
    for line in SPEC.read_text().splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## Configuration"
            continue
        if in_section:
            section.append(line)

    variables: dict[str, Documented] = {}
    for line in section:
        if not line.startswith("|"):
            continue
        cells = [_cell(c) for c in line.strip().strip("|").split("|")]
        name = cells[0]
        if not ASSIGNMENT.match(f"{name}="):
            continue  # header row, separator row
        variables[name] = Documented(required=cells[1] == "✓", default=cells[2])
    return variables


def example_variables() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        match = ASSIGNMENT.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def test_the_configuration_table_is_where_we_think_it_is() -> None:
    # Without this, restructuring the document would empty the table and every other test here
    # would pass vacuously.
    documented = documented_variables()
    assert len(documented) >= 15
    assert documented["DEVIN_API_TOKEN"].required


def test_every_documented_variable_appears_in_env_example() -> None:
    missing = sorted(set(documented_variables()) - set(example_variables()))
    assert not missing, f"documented in {SPEC.name} but absent from .env.example: {missing}"


def test_env_example_documents_every_variable_it_sets() -> None:
    extra = sorted(set(example_variables()) - set(documented_variables()))
    assert not extra, f"set in .env.example but not documented in {SPEC.name}: {extra}"


def test_optional_variables_carry_the_documented_default() -> None:
    # A variable missing from .env.example is reported by its own test; skipping it here keeps this
    # failure message about defaults rather than a KeyError.
    example = example_variables()
    wrong = {
        name: (example[name], spec.default)
        for name, spec in documented_variables().items()
        if name in example and not spec.required and spec.default and example[name] != spec.default
    }
    assert not wrong, f"value in .env.example differs from the documented default: {wrong}"


def test_required_variables_are_left_blank() -> None:
    example = example_variables()
    filled = sorted(
        name for name, spec in documented_variables().items() if spec.required and example.get(name)
    )
    assert not filled, (
        f"required variables must be blank so they get filled in deliberately: {filled}"
    )
