"""`python -m sentinel <process>` — the entry point `docker-compose.yml` starts `worker` and
`poller` with.

The `api` process is not here: uvicorn is its entry point and serves `sentinel.api.main:app`
directly, so putting a third name in this table would give it two ways to be started that could
drift apart.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence

from sentinel.pipeline import poller, worker

PROCESSES: dict[str, Callable[[], Awaitable[None]]] = {
    "worker": worker.main,
    "poller": poller.main,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the named process until it stops. `Ctrl-C` is a clean stop, not a traceback."""
    parser = argparse.ArgumentParser(prog="python -m sentinel")
    parser.add_argument("process", choices=sorted(PROCESSES))
    arguments = parser.parse_args(argv)
    try:
        asyncio.run(PROCESSES[arguments.process]())
    except KeyboardInterrupt:
        # Compose stops a container with SIGTERM and then SIGKILL; a developer stops one with
        # `Ctrl-C`. Both are how these processes are meant to end, so neither prints a stack trace.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
