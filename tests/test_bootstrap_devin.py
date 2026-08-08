"""`make bootstrap-devin`, which no one can run for real.

No Devin credentials exist ([B8](../docs/blockers.md)), so this suite is the only evidence the
script works. It is written accordingly: `respx` answers, every assertion is on the **captured
request body** or on the bytes of the `.env` it wrote, and nothing reaches the network.

Three properties carry the weight.

**Idempotence.** The script is run more than once by design, and `POST /knowledge/notes` and
`POST /schedules` create something new every time they are called. Running the whole thing twice
must leave four notes and one schedule, not eight and two — so the double run is a test, not a
paragraph, and so is resuming after a failure part-way through the notes.

**`.env` survives.** It holds real credentials and this script rewrites it. The tests pin what a
rewrite may touch (one assignment line), what it may not (every other byte, the ordering, the file
mode) and what it must refuse outright (an id that would corrupt the file, a file that is not
there).

**A refusal is reported; a fault is not.** B5 and B6 are open because nothing has ever asked Devin
whether these credentials carry enterprise scope. The reachable answer, the refused one and the
unasked one are each covered — the refused one most of all, because it is what the design expects
and what the demo depends on. So is the case that must not be folded in with it: a `401` or a `500`
is not an answer, and reporting it with a fallback would record B5 as settled by a run that never
got one.
"""

from __future__ import annotations

import importlib.util
import json
import re
import stat
import sys
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import DEVIN_TOKEN, FakeAPI, make_settings
from sentinel.config import Settings
from sentinel.devin import playbooks as pb
from sentinel.devin.client import DevinClient, registered_tag
from sentinel.devin.schemas import Capability

ROOT = Path(__file__).resolve().parents[1]
SPEC = (ROOT / "docs" / "05-devin-integration.md").read_text()


def _load_script() -> Any:
    """`scripts/bootstrap_devin.py`, imported by path.

    `scripts/` is not a package — the Makefile runs the file — so there is nothing to import by
    name. Loading it here rather than driving it as a subprocess is what lets `respx` fake Devin
    and lets the assertions read the request bodies.
    """
    location = importlib.util.spec_from_file_location(
        "bootstrap_devin", ROOT / "scripts" / "bootstrap_devin.py"
    )
    assert location is not None and location.loader is not None
    module = importlib.util.module_from_spec(location)
    sys.modules["bootstrap_devin"] = module
    location.loader.exec_module(module)
    return module


bootstrap_devin = _load_script()

ORG = "org-abc123"
ORGANIZATION = f"/v3/organizations/{ORG}"
SESSIONS = f"{ORGANIZATION}/sessions"
TAGS = f"{ORGANIZATION}/tags"
NOTES = f"{ORGANIZATION}/knowledge/notes"
SCHEDULES = f"{ORGANIZATION}/schedules"
CONSUMPTION = f"{ORGANIZATION}/consumption/daily"
ENTERPRISE_METRICS = "/v3/enterprise/metrics/sessions"

NOTE_IDS = ("note-tests", "note-lint", "note-pr", "note-paths")
SCHEDULE = "sched-nightly-1"

# A `.env` shaped like the one `make db` copies from `.env.example`: comments, blank lines, a
# required value already filled in, and the two variables this script writes.
ENV_TEXT = """\
# Sentinel configuration.

# --- Devin ---
DEVIN_API_BASE=https://api.devin.ai
DEVIN_API_TOKEN=cog_live_9f3a1c7d2b4e6f8a0c5d
DEVIN_ORG_ID=org-abc123
# JSON array, written by `make bootstrap-devin`.
DEVIN_KNOWLEDGE_IDS=

# --- GitHub ---
GITHUB_TOKEN=github_pat_11ABCDEFG0abcdefghijkl
TARGET_REPO=taxpon/superset

# Compose reads these too.
POSTGRES_PORT=54340
"""


# --- Fixtures ------------------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    """Configuration independent of the developer's own `.env`, for the values asserted on."""
    return make_settings(
        devin_org_id=ORG,
        target_repo="taxpon/superset",
        target_base_branch="master",
        autofix_label="devin:autofix",
        devin_playbook_ids={"security-fix": "pb-1", "dep-upgrade": "pb-2"},
    )


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(ENV_TEXT, encoding="utf-8")
    return path


@pytest.fixture
def env(env_path: Path) -> Any:
    """The record, reading an environment with nothing in it: the tests say what is recorded."""
    return bootstrap_devin.EnvFile(env_path, {})


@pytest.fixture
async def devin(settings: Settings, devin_api: FakeAPI) -> AsyncIterator[DevinClient]:
    """A client whose every call `devin_api` answers. `devin_api` activates the router."""
    async with DevinClient(settings) as client:
        yield client


@pytest.fixture
def wired(devin_api: FakeAPI) -> FakeAPI:
    """Devin answering every bootstrap call successfully, with no enterprise scope.

    The enterprise metrics endpoint is deliberately not registered: without `DEVIN_ENTERPRISE_ID`
    the client must not call it at all, and an unregistered route raises rather than answering.
    """
    devin_api.responds("GET", SESSIONS, 200, {"sessions": []})
    devin_api.responds("PUT", TAGS, 200, {})
    respond_with_notes(devin_api, NOTE_IDS)
    devin_api.responds("POST", SCHEDULES, 200, {"id": SCHEDULE})
    devin_api.responds("GET", CONSUMPTION, 200, {"days": [{"date": "2026-08-08", "acus": 12.5}]})
    return devin_api


@pytest.fixture
def no_recorded_ids(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """`main()` reads the real environment; a developer who exported either variable would
    otherwise change what the script decides to create."""
    for name in (bootstrap_devin.KNOWLEDGE_IDS, bootstrap_devin.SCHEDULE_ID):
        monkeypatch.delenv(name, raising=False)
    yield


def respond_with_notes(devin_api: FakeAPI, ids: Sequence[str], *then: httpx.Response) -> None:
    """Answer successive note creations with `ids`, then with `then`."""
    devin_api.route("POST", NOTES).mock(
        side_effect=[httpx.Response(200, json={"id": note_id}) for note_id in ids] + list(then)
    )


async def run(devin: DevinClient, settings: Settings, env: Any) -> Any:
    return await bootstrap_devin.bootstrap(devin, settings, env=env, out=Out())


class Out:
    """Somewhere for the report to go, which the tests that care about it can read back."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> int:
        self.lines.append(text)
        return len(text)

    @property
    def text(self) -> str:
        return "".join(self.lines)


# File access goes through these rather than being inlined: ruff's ASYNC240 rejects a `pathlib`
# call inside an `async def`, and every file here is a handful of lines under `tmp_path`.


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _raise(exc: BaseException) -> Any:
    """A call that fails: `monkeypatch.setattr(module.os, "replace", _raise(OSError(...)))`."""

    def fail(*_: Any, **__: Any) -> Any:
        raise exc

    return fail


def env_value(path: Path, name: str) -> str | None:
    """The effective assignment for `name`, read back the way `.env` would be read."""
    found = None
    for line in read_lines(path):
        if line.startswith(f"{name}="):
            found = line.split("=", 1)[1]
    return found


# --- Step 2: the tag vocabulary ------------------------------------------------------------------


async def test_registers_the_vocabulary_the_client_validates_against(
    devin: DevinClient, settings: Settings, env: Any, wired: FakeAPI
) -> None:
    """B7's mitigation: the vocabulary is registered before any session can be rejected for using
    it. It is built from `playbooks.py` rather than transcribed, so drift is impossible."""
    await run(devin, settings, env)

    sent = wired.only("PUT", TAGS)
    assert sent.json == {"tags": [pb.NAMESPACE_TAG, *(f"{p}:" for p in pb.TAG_PREFIXES)]}
    assert sent.headers["authorization"] == f"Bearer {DEVIN_TOKEN}"


def test_every_tag_sentinel_sends_is_covered_by_what_is_registered() -> None:
    """The registration and `registered_tag` read the same two exports, so a value behind any
    registered prefix is accepted by both."""
    registered = set(bootstrap_devin.VOCABULARY)
    assert pb.NAMESPACE_TAG in registered
    for prefix in pb.TAG_PREFIXES:
        assert f"{prefix}:" in registered
        assert registered_tag(f"{prefix}:whatever") == f"{prefix}:whatever"


async def test_the_vocabulary_is_registered_again_on_a_second_run(
    devin: DevinClient, settings: Settings, env: Any, wired: FakeAPI
) -> None:
    """A `PUT` of the whole set is idempotent by nature: re-sending it is how the vocabulary is
    repaired after someone edits it in the dashboard."""
    await run(devin, settings, env)
    await run(devin, settings, env)

    assert len(wired.sent("PUT", TAGS)) == 2
    assert {request.text for request in wired.sent("PUT", TAGS)} == {
        wired.sent("PUT", TAGS)[0].text
    }


# --- Step 3: the knowledge notes -----------------------------------------------------------------


def test_one_note_per_entry_in_the_spec() -> None:
    """`docs/05-devin-integration.md#knowledge-notes` lists them as a numbered list; a fifth topic
    added there without a note here fails the suite rather than being quietly unseeded."""
    section = SPEC.split("## Knowledge notes", 1)[1].split("\n## ", 1)[0]
    assert len(re.findall(r"(?m)^\d+\. ", section)) == len(bootstrap_devin.NOTES) == 4


async def test_creates_every_note_with_a_name_a_trigger_and_a_body(
    devin: DevinClient, settings: Settings, env: Any, wired: FakeAPI
) -> None:
    await run(devin, settings, env)

    sent = [request.json for request in wired.sent("POST", NOTES)]
    assert [note["name"] for note in sent] == [note.name for note in bootstrap_devin.NOTES]
    assert len({note["name"] for note in sent}) == 4
    for note in sent:
        assert note["body"].strip()
        assert note["trigger_description"].strip()


async def test_records_the_note_ids_in_env(
    devin: DevinClient, settings: Settings, env: Any, env_path: Path, wired: FakeAPI
) -> None:
    """`DEVIN_KNOWLEDGE_IDS` is what every session's `knowledge_ids` comes from, and — since no
    endpoint lists the notes — the only record that they exist at all."""
    report = await run(devin, settings, env)

    assert json.loads(env_value(env_path, bootstrap_devin.KNOWLEDGE_IDS) or "") == list(NOTE_IDS)
    assert "created 4 note(s)" in report.steps["knowledge"]


async def test_creates_nothing_when_all_four_are_already_recorded(
    devin: DevinClient, settings: Settings, env: Any, env_path: Path, wired: FakeAPI
) -> None:
    recorded = json.dumps(list(NOTE_IDS), separators=(",", ":"))
    write(env_path, ENV_TEXT.replace("DEVIN_KNOWLEDGE_IDS=", f"DEVIN_KNOWLEDGE_IDS={recorded}"))
    before = read_lines(env_path)

    report = await run(devin, settings, env)

    assert wired.sent("POST", NOTES) == []
    assert f"{len(NOTE_IDS)} note(s) already recorded" in report.steps["knowledge"]
    assert read_lines(env_path) == [*before, f"DEVIN_SCHEDULE_ID={SCHEDULE}"]


async def test_resumes_at_the_note_after_the_last_recorded_one(
    devin: DevinClient, settings: Settings, env: Any, env_path: Path, wired: FakeAPI
) -> None:
    """A run that stopped after two notes must create the third and the fourth — not four more."""
    write(
        env_path,
        ENV_TEXT.replace("DEVIN_KNOWLEDGE_IDS=", 'DEVIN_KNOWLEDGE_IDS=["kept-1","kept-2"]'),
    )

    await run(devin, settings, env)

    sent = [request.json["name"] for request in wired.sent("POST", NOTES)]
    assert sent == [note.name for note in bootstrap_devin.NOTES[2:]]
    assert json.loads(env_value(env_path, bootstrap_devin.KNOWLEDGE_IDS) or "") == [
        "kept-1",
        "kept-2",
        NOTE_IDS[0],
        NOTE_IDS[1],
    ]


# --- Step 4: the nightly sweep -------------------------------------------------------------------


async def test_creates_the_sweep_exactly_as_the_spec_tabulates_it(
    devin: DevinClient, settings: Settings, env: Any, wired: FakeAPI
) -> None:
    """Every row of `docs/05-devin-integration.md#scheduled-sweep`, and the prompt closing the loop
    back into the pipeline: an issue carrying the trigger label, and no pull request."""
    await run(devin, settings, env)

    body = wired.only("POST", SCHEDULES).json
    assert body["name"] == "sentinel-nightly-vuln-sweep"
    assert body["schedule_type"] == "recurring"
    assert body["frequency"] == "0 3 * * *"
    assert body["tags"] == ["sentinel", "class:scheduled-sweep"]
    assert body["notify_on"] == "failure"

    prompt = body["prompt"]
    assert "pip-audit" in prompt and "npm audit" in prompt
    assert "taxpon/superset" in prompt and "devin:autofix" in prompt
    assert "class:security-dep" in prompt and "class:frontend-dep" in prompt
    assert "Do not open duplicates." in prompt
    assert "Do not open a pull request" in prompt


async def test_records_the_schedule_id_and_creates_no_second_sweep(
    devin: DevinClient, settings: Settings, env: Any, env_path: Path, wired: FakeAPI
) -> None:
    """Two sweeps would file the same issues every night, and v3 has no endpoint that lists
    schedules to notice it."""
    first = await run(devin, settings, env)
    assert env_value(env_path, bootstrap_devin.SCHEDULE_ID) == SCHEDULE

    second = await run(devin, settings, env)

    assert len(wired.sent("POST", SCHEDULES)) == 1
    assert SCHEDULE in second.steps["schedule"]
    assert "nothing created" in second.steps["schedule"]
    assert "created sentinel-nightly-vuln-sweep" in first.steps["schedule"]


async def test_an_exported_variable_counts_as_recorded(
    devin: DevinClient, settings: Settings, env_path: Path, wired: FakeAPI
) -> None:
    """`pydantic-settings` resolves the environment before `.env`, so a variable exported in the
    shell is what the worker will use — and therefore what tells this script the sweep exists.

    The file says something else on purpose: when the two disagree, the exported value is the one
    in force, and reporting the file's would name a schedule nothing is actually running.
    """
    write(
        env_path,
        ENV_TEXT.replace(
            "DEVIN_KNOWLEDGE_IDS=", f"DEVIN_KNOWLEDGE_IDS={json.dumps(list(NOTE_IDS))}"
        )
        + "DEVIN_SCHEDULE_ID=sched-in-the-file\n",
    )
    env = bootstrap_devin.EnvFile(env_path, {bootstrap_devin.SCHEDULE_ID: "sched-from-the-shell"})

    report = await run(devin, settings, env)

    assert wired.sent("POST", SCHEDULES) == []
    assert "sched-from-the-shell" in report.steps["schedule"]
    assert "sched-in-the-file" not in report.steps["schedule"]


# --- Idempotence, end to end ---------------------------------------------------------------------


async def test_running_twice_leaves_four_notes_and_one_schedule(
    devin: DevinClient, settings: Settings, env: Any, env_path: Path, wired: FakeAPI
) -> None:
    await run(devin, settings, env)
    after_first = read_bytes(env_path)

    await run(devin, settings, env)

    assert len(wired.sent("POST", NOTES)) == 4
    assert len(wired.sent("POST", SCHEDULES)) == 1
    assert read_bytes(env_path) == after_first


# --- Step 1: the token ---------------------------------------------------------------------------


async def test_counts_the_organisations_sessions_and_sentinels_own(
    devin: DevinClient, settings: Settings, env: Any, devin_api: FakeAPI, wired: FakeAPI
) -> None:
    devin_api.responds(
        "GET",
        SESSIONS,
        200,
        {
            "sessions": [
                {"session_id": "s-1", "status": "exit", "tags": ["sentinel", "issue:1"]},
                {"session_id": "s-2", "status": "running", "tags": ["sentinel"]},
                {"session_id": "s-3", "status": "running", "tags": ["someone-else"]},
            ]
        },
    )

    report = await run(devin, settings, env)

    assert report.steps["token"] == "accepted — 3 session(s) visible, 2 of them Sentinel's"


async def test_a_rejected_token_creates_nothing_at_all(
    devin: DevinClient, settings: Settings, env: Any, env_path: Path, devin_api: FakeAPI
) -> None:
    """The verification runs first for this reason: a `401` found after three notes exist is three
    notes to delete by hand."""
    before = read_bytes(env_path)
    devin_api.responds("GET", SESSIONS, 401, {"detail": "invalid token"})

    with pytest.raises(bootstrap_devin.BootstrapError) as failure:
        await run(devin, settings, env)

    assert devin_api.sent("PUT") == []
    assert devin_api.sent("POST") == []
    assert read_bytes(env_path) == before
    assert failure.value.step == "step 1 of 4 (token)"
    assert "DEVIN_API_TOKEN" in failure.value.remedy


# --- Failure: legible, and never destructive -----------------------------------------------------


async def test_a_rejected_note_keeps_what_was_already_created_and_resumes(
    devin: DevinClient, settings: Settings, env: Any, env_path: Path, devin_api: FakeAPI
) -> None:
    """The record is written after each creation, so the two notes that exist are still findable
    and the next run picks up at the third rather than creating four more."""
    devin_api.responds("GET", SESSIONS, 200, {"sessions": []})
    devin_api.responds("PUT", TAGS, 200, {})
    devin_api.responds("POST", SCHEDULES, 200, {"id": SCHEDULE})
    devin_api.responds("GET", CONSUMPTION, 200, {"days": []})
    respond_with_notes(
        devin_api,
        NOTE_IDS[:2],
        httpx.Response(422, json={"detail": "trigger_description too long"}),
        *[httpx.Response(200, json={"id": note_id}) for note_id in NOTE_IDS[2:]],
    )

    with pytest.raises(bootstrap_devin.BootstrapError) as failure:
        await run(devin, settings, env)

    assert failure.value.step == "step 3 of 4 (knowledge)"
    assert json.loads(env_value(env_path, bootstrap_devin.KNOWLEDGE_IDS) or "") == list(
        NOTE_IDS[:2]
    )
    assert devin_api.sent("POST", SCHEDULES) == []
    assert ENV_TEXT.splitlines()[4] in read_lines(env_path)

    await run(devin, settings, env)

    assert len(devin_api.sent("POST", NOTES)) == 5
    assert json.loads(env_value(env_path, bootstrap_devin.KNOWLEDGE_IDS) or "") == list(NOTE_IDS)
    assert env_value(env_path, bootstrap_devin.SCHEDULE_ID) == SCHEDULE


async def test_the_failure_report_names_the_step_the_answer_and_the_remedy(
    devin: DevinClient, settings: Settings, env: Any, devin_api: FakeAPI
) -> None:
    """An operator at a terminal needs to know which step stopped and what to do; a traceback says
    neither. The token must not appear anywhere in it."""
    devin_api.responds("GET", SESSIONS, 200, {"sessions": []})
    devin_api.responds("PUT", TAGS, 403, {"detail": "missing ManageOrgSessions"})

    with pytest.raises(bootstrap_devin.BootstrapError) as failure:
        await run(devin, settings, env)

    report = failure.value.report()
    assert "step 2 of 4 (tags)" in report
    assert "403" in report and "missing ManageOrgSessions" in report
    assert "ManageOrgSessions at the organisation level" in report.replace("`", "")
    assert "what to do:" in report
    assert DEVIN_TOKEN not in report


async def test_an_unreachable_devin_says_so_and_says_the_rerun_is_safe(
    devin: DevinClient, settings: Settings, env: Any, devin_api: FakeAPI
) -> None:
    devin_api.route("GET", SESSIONS).mock(side_effect=httpx.ConnectError("nope"))

    with pytest.raises(bootstrap_devin.BootstrapError) as failure:
        await run(devin, settings, env)

    assert "could not be reached" in failure.value.problem
    assert "idempotent" in failure.value.remedy


async def test_an_unreadable_answer_points_at_the_schemas_not_the_script(
    devin: DevinClient, settings: Settings, env: Any, devin_api: FakeAPI, wired: FakeAPI
) -> None:
    """The bootstrap response shapes are unverified (B8), so a body that will not parse is the
    likeliest real failure — and it is `devin/schemas.py` that would need fixing."""
    devin_api.responds("POST", SCHEDULES, 200, {"nothing": "recognisable"})

    with pytest.raises(bootstrap_devin.BootstrapError) as failure:
        await run(devin, settings, env)

    assert failure.value.step == "step 4 of 4 (schedule)"
    assert "devin/schemas.py" in failure.value.remedy


# --- The `.env` rewrite --------------------------------------------------------------------------


async def test_nothing_but_the_recorded_lines_changes(
    devin: DevinClient, settings: Settings, env: Any, env_path: Path, wired: FakeAPI
) -> None:
    """Comments, blank lines, ordering, the operator's own variables and the credentials all
    survive; the file grows by exactly the one line that was not already there."""
    await run(devin, settings, env)

    before = ENV_TEXT.splitlines()
    after = read_lines(env_path)
    assert after[: len(before)] == [
        line if not line.startswith("DEVIN_KNOWLEDGE_IDS=") else after[before.index(line)]
        for line in before
    ]
    assert [line for line in after if line.startswith("DEVIN_KNOWLEDGE_IDS=")] == [
        f"DEVIN_KNOWLEDGE_IDS={json.dumps(list(NOTE_IDS), separators=(',', ':'))}"
    ]
    assert after[len(before) :] == [f"DEVIN_SCHEDULE_ID={SCHEDULE}"]
    assert "DEVIN_API_TOKEN=cog_live_9f3a1c7d2b4e6f8a0c5d" in after
    assert after.count("POSTGRES_PORT=54340") == 1


def test_the_effective_assignment_is_the_one_replaced(tmp_path: Path) -> None:
    """`.env` is hand-edited, and a duplicated variable is read as its last assignment. Replacing
    the first would leave the stale one winning; deleting either would be an edit nobody asked
    for."""
    path = tmp_path / ".env"
    path.write_text("DEVIN_SCHEDULE_ID=old-1\nOTHER=x\nDEVIN_SCHEDULE_ID=old-2\n", encoding="utf-8")

    bootstrap_devin.EnvFile(path, {}).record(bootstrap_devin.SCHEDULE_ID, "new")

    assert path.read_text(encoding="utf-8") == (
        "DEVIN_SCHEDULE_ID=old-1\nOTHER=x\nDEVIN_SCHEDULE_ID=new\n"
    )


def test_a_commented_out_variable_is_not_the_assignment(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# DEVIN_SCHEDULE_ID=commented\nOTHER=x", encoding="utf-8")

    bootstrap_devin.EnvFile(path, {}).record(bootstrap_devin.SCHEDULE_ID, "new")

    assert path.read_text(encoding="utf-8") == (
        "# DEVIN_SCHEDULE_ID=commented\nOTHER=x\nDEVIN_SCHEDULE_ID=new\n"
    )


def test_an_exported_assignment_is_rewritten_in_place(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("export DEVIN_SCHEDULE_ID=old\n", encoding="utf-8")

    bootstrap_devin.EnvFile(path, {}).record(bootstrap_devin.SCHEDULE_ID, "new")

    assert path.read_text(encoding="utf-8") == "DEVIN_SCHEDULE_ID=new\n"


def test_line_endings_are_left_as_they_were(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"A=1\r\nDEVIN_SCHEDULE_ID=old\r\nB=2\r\n")

    bootstrap_devin.EnvFile(path, {}).record(bootstrap_devin.SCHEDULE_ID, "new")

    assert path.read_bytes() == b"A=1\r\nDEVIN_SCHEDULE_ID=new\r\nB=2\r\n"


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644])
def test_the_file_mode_is_carried_over(tmp_path: Path, mode: int) -> None:
    """A `.env` an operator chmodded to 0600 must not come back world-readable because this script
    rewrote it — and one that was not must not be silently tightened either, since the temporary
    file this is written through is created 0600 whatever the original was."""
    path = tmp_path / ".env"
    write(path, "A=1\n")
    path.chmod(mode)

    bootstrap_devin.EnvFile(path, {}).record(bootstrap_devin.SCHEDULE_ID, "new")

    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_recording_the_same_value_does_not_rewrite_the_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("DEVIN_SCHEDULE_ID=same\n", encoding="utf-8")
    record = bootstrap_devin.EnvFile(path, {})

    assert record.record(bootstrap_devin.SCHEDULE_ID, "same") is False
    assert record.record(bootstrap_devin.SCHEDULE_ID, "other") is True


def test_no_temporary_file_is_left_behind(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    write(path, "A=1\n")

    bootstrap_devin.EnvFile(path, {}).record(bootstrap_devin.SCHEDULE_ID, "new")

    assert [child.name for child in tmp_path.iterdir()] == [".env"]


def test_a_write_that_fails_leaves_the_previous_file_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of writing through `os.replace`: the rename either happened or it did not, so an
    interrupted write cannot leave half a `.env` behind. Writing the new text over the file in
    place would pass every other test here and fail this one."""
    path = tmp_path / ".env"
    write(path, "DEVIN_SCHEDULE_ID=old\nDEVIN_API_TOKEN=cog_live_secret\n")
    monkeypatch.setattr(
        bootstrap_devin.os, "replace", _raise(OSError(28, "No space left on device"))
    )

    with pytest.raises(bootstrap_devin.BootstrapError) as failure:
        bootstrap_devin.EnvFile(path, {}).record(bootstrap_devin.SCHEDULE_ID, "new")

    assert read_lines(path) == ["DEVIN_SCHEDULE_ID=old", "DEVIN_API_TOKEN=cog_live_secret"]
    assert [child.name for child in tmp_path.iterdir()] == [".env"]
    assert "DEVIN_SCHEDULE_ID=new" in failure.value.remedy


def test_the_temporary_file_is_written_beside_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It holds a complete copy of every credential in `.env`. Somewhere else — the system
    temporary directory, which is world-listable — is a copy in a place the operator did not choose
    and would never think to clear."""
    path = tmp_path / ".env"
    write(path, "A=1\n")
    directories: list[Any] = []
    original = bootstrap_devin.tempfile.mkstemp

    def spy(**options: Any) -> Any:
        directories.append(options.get("dir"))
        return original(**options)

    monkeypatch.setattr(bootstrap_devin.tempfile, "mkstemp", spy)

    bootstrap_devin.EnvFile(path, {}).record(bootstrap_devin.SCHEDULE_ID, "new")

    assert directories == [tmp_path]


def test_a_symlinked_env_is_followed_rather_than_replaced(tmp_path: Path) -> None:
    """`os.replace` onto the link path would swap the link for a regular file: the real `.env`
    would stop receiving updates, and a second complete copy of every credential would be left
    behind at the link."""
    target = tmp_path / "real.env"
    write(target, "DEVIN_API_TOKEN=cog_live_secret\n")
    link = tmp_path / ".env"
    link.symlink_to(target)

    bootstrap_devin.EnvFile(link, {}).record(bootstrap_devin.SCHEDULE_ID, "new")

    assert link.is_symlink()
    assert read_lines(target) == ["DEVIN_API_TOKEN=cog_live_secret", "DEVIN_SCHEDULE_ID=new"]
    assert sorted(child.name for child in tmp_path.iterdir()) == [".env", "real.env"]


def test_an_unwritable_env_hands_the_id_back_instead_of_a_traceback(tmp_path: Path) -> None:
    """The id is already created at Devin by the time the record is written. An `OSError` escaping
    as a traceback would take it with it, leaving a note nobody can find again — which is the same
    harm `_no_env_file` already refuses to do."""
    path = tmp_path / ".env"
    write(path, "A=1\n")
    tmp_path.chmod(0o500)

    try:
        with pytest.raises(bootstrap_devin.BootstrapError) as failure:
            bootstrap_devin.EnvFile(path, {}).record(bootstrap_devin.KNOWLEDGE_IDS, '["note-1"]')
    finally:
        tmp_path.chmod(0o700)

    assert 'DEVIN_KNOWLEDGE_IDS=["note-1"]' in failure.value.remedy
    assert "re-run" in failure.value.remedy


def test_an_env_that_is_not_utf8_is_reported_without_quoting_it(tmp_path: Path) -> None:
    """The bytes a `UnicodeDecodeError` renders came out of a file full of credentials."""
    path = tmp_path / ".env"
    path.write_bytes(b"DEVIN_API_TOKEN=cog_\xff\xfe_secret\n")

    with pytest.raises(bootstrap_devin.BootstrapError) as failure:
        bootstrap_devin.EnvFile(path, {}).recorded(bootstrap_devin.SCHEDULE_ID)

    assert "not valid UTF-8" in failure.value.problem
    assert "cog_" not in failure.value.problem + failure.value.remedy


@pytest.mark.parametrize(
    "note_id", ["note one", "note#one", 'note"one', "note'one", "note\\one", ""]
)
async def test_an_id_that_would_corrupt_env_is_refused_and_reported(
    devin: DevinClient,
    settings: Settings,
    env: Any,
    env_path: Path,
    devin_api: FakeAPI,
    note_id: str,
) -> None:
    """An unquoted `.env` value cannot carry whitespace, `#`, a quote or a backslash, and an empty
    id names nothing — `KnowledgeNote` accepts `""`, so the guard is reachable. Writing any of them
    would corrupt a file holding every credential, so the id is handed to the operator instead;
    dropping it silently would leave a note nobody can find."""
    devin_api.responds("GET", SESSIONS, 200, {"sessions": []})
    devin_api.responds("PUT", TAGS, 200, {})
    respond_with_notes(devin_api, [note_id])
    before = read_bytes(env_path)

    with pytest.raises(bootstrap_devin.BootstrapError) as failure:
        await run(devin, settings, env)

    assert read_bytes(env_path) == before
    assert repr(note_id) in failure.value.problem
    assert failure.value.step == "step 3 of 4 (knowledge)"


async def test_a_missing_env_file_is_not_created(
    devin: DevinClient, settings: Settings, tmp_path: Path, wired: FakeAPI
) -> None:
    """A file holding nothing but one written line would look like a configuration while missing
    every credential. The ids go to the operator instead, along with what to do with them."""
    missing = tmp_path / "absent" / ".env"
    missing.parent.mkdir()

    with pytest.raises(bootstrap_devin.BootstrapError) as failure:
        await run(devin, settings, bootstrap_devin.EnvFile(missing, {}))

    assert not missing.exists()
    assert NOTE_IDS[0] in failure.value.remedy
    assert "cp .env.example .env" in failure.value.remedy


@pytest.mark.parametrize("recorded", ['{"a": 1}', "not json at all", '["note-1", 3]'])
async def test_a_record_that_is_not_a_json_array_of_ids_stops_the_run(
    devin: DevinClient, settings: Settings, env_path: Path, wired: FakeAPI, recorded: str
) -> None:
    """Reading it as "nothing recorded" would create four more notes on top of whatever the value
    was hiding. A list with a non-string in it is the same hazard: `["note-1", 3]` has a length,
    and length is what decides how many notes are outstanding."""
    env = bootstrap_devin.EnvFile(env_path, {bootstrap_devin.KNOWLEDGE_IDS: recorded})

    with pytest.raises(bootstrap_devin.BootstrapError) as failure:
        await run(devin, settings, env)

    assert wired.sent("POST", NOTES) == []
    assert bootstrap_devin.KNOWLEDGE_IDS in failure.value.problem


async def test_a_blanked_record_beside_a_recorded_schedule_refuses_to_create_a_second_set(
    devin: DevinClient, settings: Settings, env_path: Path, wired: FakeAPI
) -> None:
    """`cp .env.example .env` — which is what the missing-file remedy tells an operator to run —
    ships DEVIN_KNOWLEDGE_IDS blank. Read as a first run, that silently makes a second set of four
    notes with nothing able to tell them apart afterwards.

    The schedule is created *after* the notes, so a recorded schedule id cannot predate them: it is
    the evidence that this organisation has been here before.
    """
    write(env_path, f"{ENV_TEXT}DEVIN_SCHEDULE_ID={SCHEDULE}\n")

    with pytest.raises(bootstrap_devin.BootstrapError) as failure:
        await run(devin, settings, bootstrap_devin.EnvFile(env_path, {}))

    assert wired.sent("POST", NOTES) == []
    assert failure.value.step == "step 3 of 4 (knowledge)"
    assert bootstrap_devin.KNOWLEDGE_IDS in failure.value.remedy
    assert bootstrap_devin.SCHEDULE_ID in failure.value.remedy


def test_the_effective_assignment_is_the_one_read(tmp_path: Path) -> None:
    """The read and the write must agree on which duplicate counts. Reading the first while
    replacing the last would report a stale schedule id as current — and a schedule id read stale
    is a second nightly sweep."""
    path = tmp_path / ".env"
    write(path, "DEVIN_SCHEDULE_ID=first\nOTHER=x\nDEVIN_SCHEDULE_ID=second\n")

    assert bootstrap_devin.EnvFile(path, {}).recorded(bootstrap_devin.SCHEDULE_ID) == "second"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("DEVIN_SCHEDULE_ID=sched-9", "sched-9"),
        ('DEVIN_SCHEDULE_ID="sched-9"', "sched-9"),
        ("DEVIN_SCHEDULE_ID='sched-9'", "sched-9"),
        ("DEVIN_SCHEDULE_ID=sched-9  # the nightly sweep", "sched-9"),
        ('DEVIN_SCHEDULE_ID="sched-9"  # the nightly sweep', "sched-9"),
    ],
)
def test_a_value_is_read_the_way_dotenv_reads_it(tmp_path: Path, line: str, expected: str) -> None:
    """`dotenv` is what configures the worker, so a value read differently here is one reported to
    the operator as something the worker never sees."""
    path = tmp_path / ".env"
    write(path, f"{line}\n")

    assert bootstrap_devin.EnvFile(path, {}).recorded(bootstrap_devin.SCHEDULE_ID) == expected


# --- The capability probe ------------------------------------------------------------------------


async def test_reports_reachable_capabilities_with_the_figures_they_returned(
    settings: Settings, env: Any, wired: FakeAPI
) -> None:
    """The answer B5 has been waiting for, in the case where the credentials do carry enterprise
    scope: the panels use Devin's own accounting."""
    enterprise = make_settings(
        devin_org_id=ORG, devin_enterprise_id="ent-1", devin_playbook_ids={"security-fix": "pb-1"}
    )
    wired.responds(
        "GET",
        ENTERPRISE_METRICS,
        200,
        {"sessions_with_merged_prs_count": 7, "avg_acus_per_session": 4.25},
    )

    async with DevinClient(enterprise) as client:
        report = await run(client, enterprise, env)

    metrics = report.capability(Capability.SESSION_METRICS.value)
    assert (metrics.blocker, metrics.status) == ("B5", bootstrap_devin.REACHABLE)
    assert "7 session(s) with merged PRs" in metrics.detail
    assert "4.25 ACU per session" in metrics.detail

    spend = report.capability(Capability.ACU_SPEND.value)
    assert spend.status == bootstrap_devin.REACHABLE
    assert "1 day(s) reported, 12.50 ACU in total" in spend.detail


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [(403, "forbidden"), (404, "not_found")],
)
async def test_reports_a_refused_capability_with_the_fallback_that_replaces_it(
    settings: Settings, env: Any, wired: FakeAPI, status_code: int, reason: str
) -> None:
    """The case the whole `Degradable` design exists for. What the row has to carry is the reason,
    the status and the fallback sentence the dashboard labels the derived figure with — that is
    what turns B5 from an open question into a recorded fact."""
    enterprise = make_settings(
        devin_org_id=ORG, devin_enterprise_id="ent-1", devin_playbook_ids={"security-fix": "pb-1"}
    )
    wired.responds("GET", ENTERPRISE_METRICS, status_code, {"detail": "no"})
    wired.responds("GET", CONSUMPTION, status_code, {"detail": "no"})

    async with DevinClient(enterprise) as client:
        report = await run(client, enterprise, env)

    for capability in (Capability.SESSION_METRICS, Capability.ACU_SPEND):
        row = report.capability(capability.value)
        # Returning at all is the assertion that a refusal does not fail the run.
        assert row.status == bootstrap_devin.DEGRADED
        assert f"{reason} ({status_code})" in row.detail
        assert capability.fallback in row.detail


async def test_an_unset_enterprise_id_leaves_b5_open_rather_than_degraded(
    devin: DevinClient, settings: Settings, env: Any, wired: FakeAPI
) -> None:
    """`.env.example` ships DEVIN_ENTERPRISE_ID blank, so this is the default run. Nothing is asked
    — `wired` registers no route for the endpoint, and a request would raise — and B5 asks whether
    the *token* carries `ViewAccountMetrics`, which an unset id says nothing about. Reporting it as
    `degraded` would let an operator close B5 on a run that made no request."""
    report = await run(devin, settings, env)

    row = report.capability(Capability.SESSION_METRICS.value)
    assert row.status == bootstrap_devin.NOT_PROBED
    assert "DEVIN_ENTERPRISE_ID is unset" in row.detail
    assert "B5 stays open" in row.detail
    assert Capability.SESSION_METRICS.fallback in row.detail

    playbooks = report.capability(Capability.PLAYBOOK_CREATION.value)
    assert "B6 stays open with B5" in playbooks.detail


async def test_a_configured_enterprise_id_points_b6_at_the_probed_row(
    settings: Settings, env: Any, wired: FakeAPI
) -> None:
    """With the id set the run does ask, so B6 may lean on what B5's row found."""
    enterprise = make_settings(
        devin_org_id=ORG, devin_enterprise_id="ent-1", devin_playbook_ids={"security-fix": "pb-1"}
    )
    wired.responds("GET", ENTERPRISE_METRICS, 403, {"detail": "no"})

    async with DevinClient(enterprise) as client:
        report = await run(client, enterprise, env)

    assert "session_metrics above" in report.capability(Capability.PLAYBOOK_CREATION.value).detail


@pytest.mark.parametrize(
    ("status_code", "advice"),
    [(401, "DEVIN_API_TOKEN"), (500, "docs/05-devin-integration.md")],
)
async def test_a_fault_on_the_probe_claims_no_fallback_and_does_not_exit_zero(
    devin: DevinClient,
    settings: Settings,
    env: Any,
    env_path: Path,
    wired: FakeAPI,
    status_code: int,
    advice: str,
) -> None:
    """`client.py` converts `403` and `404` into `Unavailable` and raises everything else — `401`
    deliberately, because "silently falling back to derived figures would hide it". Anything that
    reaches this handler is therefore a fault, and answering it with the fallback here would undo
    that decision one layer up: an operator would record B5 as answered ("use the derived figures")
    when the answer is "fix your token and ask again"."""
    out = Out()
    wired.responds("GET", CONSUMPTION, status_code, {"detail": "boom"})

    with pytest.raises(bootstrap_devin.BootstrapError) as failure:
        await bootstrap_devin.bootstrap(devin, settings, env=env, out=out)

    row = next(line for line in out.text.splitlines() if Capability.ACU_SPEND.value in line)
    assert bootstrap_devin.FAULT in row
    assert "unanswered" in row and str(status_code) in row
    assert Capability.ACU_SPEND.fallback not in row
    assert "fault rather than a refusal" in failure.value.remedy
    # The remedy is the one diagnosed from what came back, not a sentence about faults in general.
    assert advice in failure.value.remedy

    # Raised *after* the whole table, not instead of it: the four steps did succeed, and the rows
    # that were answered are the answer the run was for. Only the exit status changes.
    assert bootstrap_devin.CAPABILITY_HEADING.strip() in out.text
    for name in ("session_metrics", "acu_spend", "playbook_creation", "tag_vocabulary"):
        assert name in out.text
    assert "[4/4] schedule" in out.text
    assert env_value(env_path, bootstrap_devin.SCHEDULE_ID) == SCHEDULE


async def test_a_rejected_token_on_the_enterprise_probe_names_the_token(
    settings: Settings, env: Any, wired: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    """The case B5 exists for. Step 1 verifies the token against the *organisation* endpoints, so a
    401 here is the difference between "wrong token" and "no enterprise scope" — and only one of
    those is answered by writing down the fallback."""
    enterprise = make_settings(
        devin_org_id=ORG, devin_enterprise_id="ent-1", devin_playbook_ids={"security-fix": "pb-1"}
    )
    wired.responds("GET", ENTERPRISE_METRICS, 401, {"detail": "invalid token"})

    async with DevinClient(enterprise) as client:
        with pytest.raises(bootstrap_devin.BootstrapError) as failure:
            await run(client, enterprise, env)

    assert "DEVIN_API_TOKEN" in failure.value.remedy
    assert Capability.SESSION_METRICS.fallback not in failure.value.problem


async def test_playbook_creation_is_reported_without_being_probed(
    devin: DevinClient, settings: Settings, env: Any, wired: FakeAPI
) -> None:
    """B6's endpoint is deliberately absent from the client, so what is worth reporting is whether
    the fallback that replaces it is actually configured."""
    report = await run(devin, settings, env)

    row = report.capability(Capability.PLAYBOOK_CREATION.value)
    assert (row.blocker, row.status) == ("B6", bootstrap_devin.NOT_PROBED)
    assert "2 playbook id(s) configured in DEVIN_PLAYBOOK_IDS" in row.detail
    assert Capability.PLAYBOOK_CREATION.fallback in row.detail
    assert [request.path for request in wired.requests if "playbook" in request.path] == []


async def test_the_report_prints_a_row_for_every_open_blocker(
    devin: DevinClient, settings: Settings, env: Any, wired: FakeAPI
) -> None:
    """The point of the run: B5, B6 and B7 answered on one screen, before the demo."""
    out = Out()
    report = await bootstrap_devin.bootstrap(devin, settings, env=env, out=out)

    assert {row.blocker for row in report.capabilities} == {"B5", "B6", "B7", "—"}
    for row in report.capabilities:
        assert f"{row.blocker}" in out.text
        assert row.name in out.text
        assert row.status in out.text
    assert "[4/4] schedule" in out.text
    assert report.capability("tag_vocabulary").status == bootstrap_devin.REGISTERED


# --- The entry point -----------------------------------------------------------------------------


def test_main_reports_success_without_touching_stdout_with_diagnostics(
    settings: Settings,
    env_path: Path,
    wired: FakeAPI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_recorded_ids: None,
) -> None:
    monkeypatch.setattr(bootstrap_devin, "get_settings", lambda: settings)

    assert bootstrap_devin.main(["--env", str(env_path)]) == 0

    captured = capsys.readouterr()
    assert "[1/4] token" in captured.out
    assert Capability.SESSION_METRICS.value in captured.out
    assert DEVIN_TOKEN not in captured.out + captured.err
    assert env_value(env_path, bootstrap_devin.SCHEDULE_ID) == SCHEDULE


def test_main_records_into_dot_env_when_no_path_is_given(
    settings: Settings,
    wired: FakeAPI,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_recorded_ids: None,
) -> None:
    """`make bootstrap-devin` passes no `--env`, so the default is the path every real run uses and
    the one no other test here exercises."""
    monkeypatch.setattr(bootstrap_devin, "get_settings", lambda: settings)
    monkeypatch.chdir(tmp_path)
    write(tmp_path / ".env", ENV_TEXT)

    assert bootstrap_devin.main([]) == 0

    assert env_value(tmp_path / ".env", bootstrap_devin.SCHEDULE_ID) == SCHEDULE


def test_main_reports_an_unwritable_record_without_a_traceback(
    settings: Settings,
    tmp_path: Path,
    wired: FakeAPI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_recorded_ids: None,
) -> None:
    """An `OSError` from the write is the one failure that reaches `main` from outside the client.
    Escaping as a traceback would defeat the contract twice over: no step named, and the id of the
    note that was created nowhere in the output."""
    path = tmp_path / ".env"
    write(path, "A=1\n")
    monkeypatch.setattr(bootstrap_devin, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap_devin.os, "replace", _raise(OSError(13, "Permission denied")))

    assert bootstrap_devin.main(["--env", str(path)]) == 1

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Permission denied" in captured.err
    assert NOTE_IDS[0] in captured.err
    assert DEVIN_TOKEN not in captured.out + captured.err


def test_main_exits_nonzero_with_a_legible_failure_and_no_traceback(
    settings: Settings,
    env_path: Path,
    devin_api: FakeAPI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_recorded_ids: None,
) -> None:
    monkeypatch.setattr(bootstrap_devin, "get_settings", lambda: settings)
    devin_api.responds("GET", SESSIONS, 401, {"detail": "invalid token"})

    assert bootstrap_devin.main(["--env", str(env_path)]) == 1

    captured = capsys.readouterr()
    assert "make bootstrap-devin failed at step 1 of 4 (token)" in captured.err
    assert "what to do:" in captured.err
    assert "Traceback" not in captured.err
    assert DEVIN_TOKEN not in captured.out + captured.err


def test_main_reports_a_bad_configuration_distinctly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A configuration that does not describe a usable deployment is not a bootstrap failure, and
    its message already names the variables — so it is passed through rather than wrapped."""

    def raise_configuration_error() -> Settings:
        from sentinel.config import ConfigurationError

        raise ConfigurationError("invalid configuration — DEVIN_ORG_ID: Field required")

    monkeypatch.setattr(bootstrap_devin, "get_settings", raise_configuration_error)

    assert bootstrap_devin.main(["--env", str(tmp_path / ".env")]) == 2
    assert "DEVIN_ORG_ID" in capsys.readouterr().err


def test_the_script_does_not_declare_an_isolated_environment() -> None:
    """`uv run` reads a PEP 723 header as "resolve this file's own dependencies in an isolated
    environment", which would not contain `sentinel`. The Makefile runs it in the project
    environment, which is what the absence of a header selects."""
    source = (ROOT / "scripts" / "bootstrap_devin.py").read_text()
    assert "# /// script" not in source
    assert "$(UV) run scripts/bootstrap_devin.py" in (ROOT / "Makefile").read_text()
