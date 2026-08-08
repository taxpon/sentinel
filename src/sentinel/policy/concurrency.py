"""The concurrency gate: how many Devin sessions may be working at once.

`docs/06-event-pipeline.md#reliability-policy` states it exactly — count the remediations in
`SESSION_CREATED`/`RUNNING`, and at or above `MAX_CONCURRENT_SESSIONS` hold the job back. Those two
states are the ones where Devin is doing work and burning ACUs; a remediation waiting on CI or on a
reviewer holds a session open but is not consuming anything, and counting it would cap the pipeline
on human latency rather than on Devin's.

The gate is a *temporary* verdict, which is what makes deferral rather than failure the right
answer, and `sentinel.queue.defer()` the right call — see `admission.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.config import Settings, get_settings
from sentinel.models import Remediation
from sentinel.pipeline.state import State

IN_FLIGHT_STATES: Final[frozenset[State]] = frozenset({State.SESSION_CREATED, State.RUNNING})
"""The states the spec counts against `MAX_CONCURRENT_SESSIONS`."""


@dataclass(frozen=True, slots=True)
class Capacity:
    """How many sessions are working, against how many may be.

    The two numbers travel together because the decision and the explanation are the same fact: an
    operator reading "deferred" off a log line wants to know what the cap was at the time, and a
    deferral that carried only a boolean would leave them to guess.
    """

    in_flight: int
    limit: int

    @property
    def available(self) -> bool:
        """Whether one more session may start now.

        "At or above the cap" is what holds a job back, so the comparison is strictly `<`: with the
        default of three, the fourth job waits.
        """
        return self.in_flight < self.limit


async def sessions_in_flight(session: AsyncSession) -> int:
    """How many remediations have Devin working on them right now."""
    return (
        await session.execute(
            select(func.count())
            .select_from(Remediation)
            .where(Remediation.state.in_([str(state) for state in IN_FLIGHT_STATES]))
        )
    ).scalar_one()


async def capacity(session: AsyncSession, *, settings: Settings | None = None) -> Capacity:
    """The concurrency gate as it stands right now."""
    settings = get_settings() if settings is None else settings
    return Capacity(
        in_flight=await sessions_in_flight(session), limit=settings.max_concurrent_sessions
    )
