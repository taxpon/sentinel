"""Builders for the five tables of `docs/03-data-model.md`, and the recorded GitHub payloads.

Every builder returns a model with only the columns the schema requires, filled with values that
are valid together, and takes keyword overrides for the ones a test is actually about:

    session.add(a_remediation(state="RUNNING", cycle=2))

Nothing here touches the database. The row is written by the test, through the `session` fixture in
`conftest.py`, so a test that wants two remediations in one transaction gets to say so.

The defaults line up with the payloads in `fixtures/github/`: the issue is the one in
`issues.labeled.json` and the pull request is the one in `pull_request.opened.json`, so a test can
seed a remediation with the factory and then replay a recorded delivery against it without
restating either number.
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from sentinel.models import AcuLedger, Job, Remediation, RemediationEvent, WebhookDelivery

PAYLOAD_DIR = Path(__file__).parent / "fixtures" / "github"

GITHUB_PAYLOADS: tuple[str, ...] = tuple(sorted(path.stem for path in PAYLOAD_DIR.glob("*.json")))
"""Every recorded payload, by name — for a test that iterates the event mapping table."""

REPO = "taxpon/superset"
ISSUE_NUMBER = 42876
ISSUE_CLASS = "security"
PR_NUMBER = 42889
DELIVERY_ID = "0b1c8e40-7d3a-11f1-9c2e-4f6a1d8b3e57"

# Fixed and in the past, so that a duration computed from these rows is the same on every run and
# on every machine. The recorded payloads carry timestamps from the same two days.
LABELED_AT = datetime.datetime(2026, 8, 7, 15, 0, tzinfo=datetime.UTC)
DAY = LABELED_AT.date()


def github_payload(name: str) -> dict[str, Any]:
    """One recorded delivery body, by file name — `github_payload("issues.labeled")`.

    A fresh object every call: a test that mutates a payload to make an unknown-event or a
    tampered-body case cannot change what the next test loads.
    """
    payload: dict[str, Any] = json.loads((PAYLOAD_DIR / f"{name}.json").read_text())
    return payload


def a_webhook_delivery(**overrides: Any) -> WebhookDelivery:
    """A stored delivery, before it has been interpreted (`processed_at` and result unset)."""
    return WebhookDelivery(
        **{
            "delivery_id": DELIVERY_ID,
            "event": "issues",
            "action": "labeled",
            "payload": {"action": "labeled", "issue": {"number": ISSUE_NUMBER}},
            "received_at": LABELED_AT,
            **overrides,
        }
    )


def a_remediation(**overrides: Any) -> Remediation:
    """A remediation in its first state, `QUEUED`, with no session and no pull request yet."""
    return Remediation(
        **{
            "repo": REPO,
            "issue_number": ISSUE_NUMBER,
            "issue_class": ISSUE_CLASS,
            "state": "QUEUED",
            "labeled_at": LABELED_AT,
            **overrides,
        }
    )


def a_remediation_event(*, remediation_id: int, **overrides: Any) -> RemediationEvent:
    """One row of the append-only log. `remediation_id` has no sensible default: the foreign key
    is enforced, so the remediation has to exist and the test has to have flushed it."""
    return RemediationEvent(
        **{
            "remediation_id": remediation_id,
            "from_state": None,
            "to_state": "QUEUED",
            "kind": "transition",
            **overrides,
        }
    )


def a_job(**overrides: Any) -> Job:
    """A pending job, claimable immediately — `run_after` takes its `now()` server default."""
    return Job(
        **{
            "kind": "create_session",
            "payload": {"issue_number": ISSUE_NUMBER},
            "status": "pending",
            **overrides,
        }
    )


def an_acu_ledger_entry(**overrides: Any) -> AcuLedger:
    """One day of ACU consumption, as the sync job writes it."""
    return AcuLedger(
        **{
            "day": DAY,
            "acus": Decimal("12.500"),
            "synced_at": LABELED_AT,
            **overrides,
        }
    )
