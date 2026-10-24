"""Correlation id for one unit of work (an agent run, an API request), carried in a contextvar.

Every outbound call logs it and sends it as ``X-Correlation-ID``, so a run can be followed across services and
logs. O1 links it to the trace id.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

CORRELATION_HEADER = "X-Correlation-ID"
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


@contextmanager
def correlation(correlation_id: str | None = None) -> Iterator[str]:
    """``with correlation(run_id):``: everything inside, including asyncio tasks it starts, shares the id."""
    value = correlation_id or uuid.uuid4().hex
    token = _correlation_id.set(value)
    try:
        yield value
    finally:
        _correlation_id.reset(token)
