"""Retry policy: which failures to retry, and how long to wait (story T1).

YOUR PART: ``RetryPolicy.is_retryable`` and ``RetryPolicy.backoff``. The contract is in tests/test_retry.py.
``parse_retry_after`` is provided.
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from release_copilot.integrations.errors import IntegrationError

# RFC 9110: safe or idempotent methods; repeating them has the same effect as sending once.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """``Retry-After`` as seconds to wait: either delta-seconds (``"3"``) or an HTTP date. None if absent or invalid."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4  # total attempts, including the first
    base_delay: float = 0.25  # seconds; the backoff cap before jitter doubles per attempt
    max_delay: float = 8.0  # seconds; cap for computed backoff (a server's Retry-After is honoured as given)
    deadline_seconds: float = 30.0  # total budget for one logical call, all attempts and waits included
    rng: random.Random = field(default_factory=random.Random, compare=False, repr=False)

    def is_retryable(self, method: str, error: "IntegrationError", *, idempotent: bool | None = None) -> bool:
        """Retry only errors marked retryable, and only when repeating the request is safe.

        ``idempotent=None`` decides by HTTP method; ``True`` opts a POST in (the caller guarantees safety, e.g. an
        idempotency key); ``False`` forbids retries even for GET.
        """
        safe_to_repeat = method.upper() in IDEMPOTENT_METHODS if idempotent is None else idempotent
        return error.retryable and safe_to_repeat

    def backoff(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1-based) before the next one.

        A server-provided ``Retry-After`` wins. Otherwise use exponential backoff with *full jitter*.
        """
        if retry_after is not None:
            return retry_after
        cap = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return self.rng.uniform(0.0, cap)
