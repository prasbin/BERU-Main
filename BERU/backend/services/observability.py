"""Process-local request observability counters.

A single :class:`RequestMetricsTracker` lives for the lifetime of the process and
records, for every HTTP request that flows through the app:

- the total request count,
- a 60-second rolling window used to derive a recent request *rate*,
- a histogram of status codes, and
- per-``error_type`` counts for the machine-readable error envelopes returned by
  the API (see :mod:`backend.core.errors`).

The tracker is intentionally tiny and lock-free in practice: HTTP middleware runs
on the event loop, so all mutations happen sequentially on one thread. The ring
buffer is pruned lazily on read, so the memory footprint stays bounded.

Values are deliberately **process-local**: they reset on restart, and multiple
worker processes each report their own numbers. For a single-process deployment
(the common BERU case) the aggregation is exact.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

_RATE_WINDOW_SECONDS = 60.0


class RequestMetricsTracker:
    """Aggregate HTTP request counters for the current process."""

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.total: int = 0
        self.by_status: dict[int, int] = {}
        self.by_error_type: dict[str, int] = {}
        # (monotonic timestamp, status_code) pairs within the rolling window.
        self._recent: deque[tuple[float, int]] = deque()

    def record(self, status_code: int, error_type: str | None = None) -> None:
        """Record one completed HTTP response.

        Args:
            status_code: The response status code.
            error_type: Machine-readable ``error.type`` when the response was an
                API error envelope; ``None`` otherwise.
        """
        self.total += 1
        self.by_status[status_code] = self.by_status.get(status_code, 0) + 1
        if error_type:
            self.by_error_type[error_type] = self.by_error_type.get(error_type, 0) + 1
        self._recent.append((time.monotonic(), status_code))

    def rate_per_1m(self) -> float:
        """Requests completed in the trailing 60 seconds."""
        cutoff = time.monotonic() - _RATE_WINDOW_SECONDS
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()
        return float(len(self._recent))

    def snapshot(self) -> dict[str, Any]:
        """Serialisable view of the counters (for ``/metrics`` and tests)."""
        return {
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "total_requests": self.total,
            "requests_per_1m": round(self.rate_per_1m(), 1),
            "by_status": {str(code): n for code, n in sorted(self.by_status.items())},
            "by_error_type": dict(sorted(self.by_error_type.items())),
        }

    def reset(self) -> None:
        """Clear all counters (test isolation; also exported via reset_*())."""
        self.__init__()


_tracker = RequestMetricsTracker()


def get_request_tracker() -> RequestMetricsTracker:
    """Return the process-wide metrics tracker."""
    return _tracker


def reset_request_metrics() -> None:
    """Reset the process-wide tracker (used by the test autouse teardown)."""
    _tracker.reset()