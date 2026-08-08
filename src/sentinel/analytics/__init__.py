"""Analytics over the remediation tables — `docs/07-observability.md`.

`metrics.summary()` produces the whole `GET /api/analytics/summary` payload; the API layer serves
it without computing anything of its own.
"""

from sentinel.analytics.metrics import DEFAULT_WINDOW, SummaryJson, Window, parse_window, summary

__all__ = ["DEFAULT_WINDOW", "SummaryJson", "Window", "parse_window", "summary"]
