"""`python -m sentinel <process>` — the entry point `docker-compose.yml` starts `worker` and
`poller` with.

The `api` process is not here: uvicorn is its entry point and serves `sentinel.api.main:app`
directly, so putting a third name in this table would give it two ways to be started that could
drift apart.

**Both processes stop on a signal rather than being killed mid-job.** Compose and Fly both end a
container by sending `SIGTERM` and then, after a grace period, `SIGKILL`. Ignoring the first means
every stop lands on the second: a worker holding a job dies with its lease still held, and no other
worker may touch that job until `JOB_LEASE_TIMEOUT_SECONDS` — fifteen minutes — has passed. A deploy
would stall an in-flight remediation for a quarter of an hour, silently.

So the signal sets the `stop` event the two loops already take. The loop stops claiming, the job or
tick in hand finishes and releases its lease, and the connection pool is disposed on the way out.

What this does not do is interrupt a job that is already running. A job still in progress when the
grace period expires is killed as before and falls back to the lease — the guarantee that makes that
recoverable rather than lost. Handling that case would mean cancelling a Devin or GitHub call
mid-flight, which is a larger decision than this one.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections.abc import Awaitable, Sequence
from typing import Protocol

from sentinel.observability.logging import get_logger
from sentinel.pipeline import poller, worker

log = get_logger(__name__)


class Process(Protocol):
    """A long-running process: runs until `stop` is set."""

    def __call__(self, *, stop: asyncio.Event) -> Awaitable[None]: ...


PROCESSES: dict[str, Process] = {
    "worker": worker.main,
    "poller": poller.main,
}

STOP_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)
"""What an orderly stop looks like. `SIGTERM` is Compose and Fly; `SIGINT` is a developer's
`Ctrl-C`. `SIGKILL` cannot be caught by anyone, which is why the grace period matters."""


async def serve(process: Process) -> None:
    """Run `process` until it finishes or a stop signal arrives."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in STOP_SIGNALS:
        # `add_signal_handler` rather than `signal.signal`: it wakes the event loop, where a plain
        # handler would only run once the loop next scheduled Python — which, for a worker asleep on
        # an empty queue, is after the idle delay it is already waiting out.
        loop.add_signal_handler(number, _requested, stop, number)
    try:
        await process(stop=stop)
    finally:
        for number in STOP_SIGNALS:
            loop.remove_signal_handler(number)


def _requested(stop: asyncio.Event, number: signal.Signals) -> None:
    """Ask the loop to finish what it is doing. Logged, because a stop nobody asked for is worth
    seeing in the container's last lines — a platform reclaiming a machine looks exactly like this.
    """
    if not stop.is_set():
        log.info("process.stopping", signal=signal.Signals(number).name)
    stop.set()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the named process until it stops. A signal is a clean stop, not a traceback."""
    parser = argparse.ArgumentParser(prog="python -m sentinel")
    parser.add_argument("process", choices=sorted(PROCESSES))
    arguments = parser.parse_args(argv)
    try:
        asyncio.run(serve(PROCESSES[arguments.process]))
    except KeyboardInterrupt:
        # Reachable only where the handler above could not be installed — `add_signal_handler` is
        # not available on every platform, and asyncio falls back to raising this. The containers
        # are Linux and take the path above; this keeps a developer elsewhere from seeing a
        # traceback for pressing `Ctrl-C`.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
