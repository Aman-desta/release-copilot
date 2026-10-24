"""Per-service circuit breaker.

closed ── N consecutive failures ──▶ open ── reset_timeout elapsed ──▶ half-open
  ▲                                   ▲                                   │
  └───────── trial call succeeds ─────┼──────── trial call fails ─────────┘

While open, calls fail immediately with ``CircuitOpenError`` (no HTTP), which protects a struggling upstream
and keeps agents from burning their budgets on timeouts. Half-open lets exactly one trial call through.
Only upstream failures count (5xx, timeouts, connection errors); 4xx are our own mistakes, not upstream health.
"""

import time
from collections.abc import Callable
from enum import Enum

from release_copilot.integrations.errors import CircuitOpenError


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        service: str,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.service = service
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self.reset_timeout:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def before_call(self, method: str = "", path: str = "") -> None:
        state = self.state
        if state is BreakerState.OPEN or (state is BreakerState.HALF_OPEN and self._trial_in_flight):
            assert self._opened_at is not None
            wait = max(0.0, self.reset_timeout - (self._clock() - self._opened_at))
            raise CircuitOpenError(
                f"circuit open after {self._failures} consecutive failures; next trial in {wait:.0f}s",
                service=self.service,
                method=method,
                path=path,
                retry_after=wait,
            )
        if state is BreakerState.HALF_OPEN:
            self._trial_in_flight = True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._trial_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        if self._trial_in_flight or self._failures >= self.failure_threshold:
            self._opened_at = self._clock()  # (re)open
        self._trial_in_flight = False

    def release_trial(self) -> None:
        """A trial call ended without a verdict on upstream health (e.g. a 404): let the next call try."""
        self._trial_in_flight = False
