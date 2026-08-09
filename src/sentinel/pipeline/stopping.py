"""Waiting that a stop can cut short.

The worker and the poller both spend most of their life asleep — the worker for a second between
empty polls, the poller for `POLL_INTERVAL_SECONDS` between ticks. Checking `stop` only at the top
of the loop means a signal arriving during that sleep is not noticed until it ends, and the platform
does not wait: Compose allows ten seconds before `SIGKILL` and the poller's interval is twenty. The
process is then killed exactly as if it had ignored the signal, having logged that it received it.

That is not hypothetical. It is what the first version of this did, and the poller's container came
back with exit code 137 while the worker's — whose delay happens to be one second — came back with
zero.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

Sleep = Callable[[float], Awaitable[None]]


async def sleep_or_stop(seconds: float, *, stop: asyncio.Event | None, sleep: Sleep) -> None:
    """Wait `seconds`, or until `stop` is set — whichever comes first.

    `sleep` is still the one that is awaited, rather than being replaced by a timeout on the event,
    because it is injected by the tests to drive the cadence without living through it. Racing the
    two keeps that: a test's `sleep` is entered on every wait exactly as before, and a real one is
    abandoned the moment a stop arrives.
    """
    if stop is None:
        await sleep(seconds)
        return

    stopping = asyncio.ensure_future(stop.wait())
    sleeping = asyncio.ensure_future(sleep(seconds))
    try:
        await asyncio.wait({stopping, sleeping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Both are cancelled rather than only the loser: `asyncio.wait` leaves a finished task's
        # result unretrieved, and cancelling a task that is already done is a no-op.
        for task in (stopping, sleeping):
            task.cancel()
