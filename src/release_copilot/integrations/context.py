"""Run context for outbound calls, carried in contextvars so every call inside a run picks it up.

- The correlation id of one unit of work (an agent run, an API request): every outbound call logs it and sends it
  as ``X-Correlation-ID``, so a run can be followed across services and logs. O1 links it to the trace id.
- The mock scenario, for evals against the mock services only: sent as ``X-Mock-Scenario``, so runs of different
  scenarios can share one mock server in parallel. It is never set in production (``agents.run`` refuses it there).
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

CORRELATION_HEADER = "X-Correlation-ID"
SCENARIO_HEADER = "X-Mock-Scenario"
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_scenario: ContextVar[str | None] = ContextVar("mock_scenario", default=None)


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


def get_scenario() -> str | None:
    return _scenario.get()


@contextmanager
def mock_scenario(name: str | None) -> Iterator[str | None]:
    """``with mock_scenario("critical_cve"):``: calls inside ask the mock services for that scenario. None: no header."""
    token = _scenario.set(name)
    try:
        yield name
    finally:
        _scenario.reset(token)
