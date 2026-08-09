"""`sleep_or_stop` — the wait that a stop can cut short.

The loops spend most of their life here rather than working, so this is where a stop signal actually
lands. Checking `stop` only at the top of the loop left the poller asleep for its twenty-second
interval while Compose counted to ten and killed it: the container came back with exit code 137,
having logged that it received the signal it then ignored.
"""

from __future__ import annotations

import asyncio

from sentinel.pipeline.stopping import sleep_or_stop


async def test_a_stop_already_set_does_not_wait_out_the_interval() -> None:
    """The poller's interval is twenty seconds and Compose allows ten. Waiting it out is the bug."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await asyncio.sleep(seconds)

    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(sleep_or_stop(30, stop=stop, sleep=sleep), timeout=5)


async def test_a_stop_arriving_mid_wait_ends_it() -> None:
    """The ordinary case: the process is idle when the platform asks it to go."""
    stop = asyncio.Event()

    async def request() -> None:
        await asyncio.sleep(0)
        stop.set()

    waiting = asyncio.ensure_future(sleep_or_stop(30, stop=stop, sleep=asyncio.sleep))
    await request()
    await asyncio.wait_for(waiting, timeout=5)


async def test_the_injected_sleep_is_the_one_awaited() -> None:
    """Both loops' tests drive their cadence by injecting `sleep`, and would stop measuring
    anything if this waited on the event with a timeout instead of racing the two."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    await sleep_or_stop(20, stop=asyncio.Event(), sleep=sleep)

    assert slept == [20]


async def test_without_a_stop_it_is_just_the_sleep() -> None:
    """`run()` may be called with no stop at all: a test, or a process nobody is managing."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    await sleep_or_stop(7, stop=None, sleep=sleep)

    assert slept == [7]


async def test_a_cancelled_wait_leaves_nothing_running() -> None:
    """Two tasks are started per wait, once per second in the worker. One leaked per wait is a leak
    that grows for as long as the process runs."""
    stop = asyncio.Event()
    before = len(asyncio.all_tasks())

    waiting = asyncio.ensure_future(sleep_or_stop(30, stop=stop, sleep=asyncio.sleep))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(waiting, timeout=5)
    await asyncio.sleep(0)

    assert len(asyncio.all_tasks()) <= before + 1  # this test's own task
