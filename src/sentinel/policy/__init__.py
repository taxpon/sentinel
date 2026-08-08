"""The decisions the worker makes before it spends anything.

The reliability policy table of `docs/06-event-pipeline.md` has four rows about limits. Two of them
are the queue's (`retry` and `non-retryable`, in `sentinel.queue`) and two are here — concurrency
and the daily ACU budget — together with the caller-side half of deduplication, which the database
enforces and this package only has to use correctly.

`admit_session()` is the whole of it from a caller's point of view; `admission.py` documents the
order the checks are made in and what has already happened to the job by the time each `Decision`
comes back. The three modules underneath it are readable on their own:

- `dedup.py` — create-if-absent, atomic against a concurrent inserter.
- `concurrency.py` — how many sessions are working, and whether one more may start.
- `budget.py` — what today has cost according to each source that knows, and whether one more
  session fits inside `DAILY_ACU_BUDGET`.
"""

from sentinel.policy.admission import (
    CONCURRENCY_CAP_REACHED,
    REMEDIATION_TERMINAL,
    SESSION_ALREADY_CREATED,
    Decision,
    Verdict,
    admit_session,
    block,
)
from sentinel.policy.budget import (
    BUDGET_EXHAUSTED,
    ConsumptionSource,
    DailySpend,
    budget_allows,
    daily_spend,
)
from sentinel.policy.concurrency import (
    IN_FLIGHT_STATES,
    Capacity,
    capacity,
    sessions_in_flight,
)
from sentinel.policy.dedup import Upsert, ensure_remediation

__all__ = [
    "BUDGET_EXHAUSTED",
    "CONCURRENCY_CAP_REACHED",
    "IN_FLIGHT_STATES",
    "REMEDIATION_TERMINAL",
    "SESSION_ALREADY_CREATED",
    "Capacity",
    "ConsumptionSource",
    "DailySpend",
    "Decision",
    "Upsert",
    "Verdict",
    "admit_session",
    "block",
    "budget_allows",
    "capacity",
    "daily_spend",
    "ensure_remediation",
    "sessions_in_flight",
]
