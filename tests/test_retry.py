"""Spec for ``RetryPolicy`` (story T1). Make these pass."""

import random
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from release_copilot.integrations.errors import (
    AuthError,
    InvalidRequestError,
    NotFoundError,
    RateLimitedError,
    UpstreamUnavailableError,
)
from release_copilot.integrations.retry import RetryPolicy, parse_retry_after


def err(cls, retry_after=None):
    return cls("boom", service="test", retry_after=retry_after)


# ----------------------------------------------------------------------------------- is_retryable


@pytest.mark.parametrize("method", ["GET", "get", "HEAD", "OPTIONS", "PUT", "DELETE"])
def test_idempotent_methods_retry_transient_errors(method):
    policy = RetryPolicy()
    assert policy.is_retryable(method, err(UpstreamUnavailableError))
    assert policy.is_retryable(method, err(RateLimitedError))


@pytest.mark.parametrize("cls", [AuthError, NotFoundError, InvalidRequestError])
def test_client_errors_are_never_retried(cls):
    assert not RetryPolicy().is_retryable("GET", err(cls))
    assert not RetryPolicy().is_retryable("POST", err(cls), idempotent=True)


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_non_idempotent_methods_are_not_retried_by_default(method):
    # A POST that timed out may have succeeded upstream: retrying could create a second change request.
    assert not RetryPolicy().is_retryable(method, err(UpstreamUnavailableError))


def test_callers_can_opt_in_or_out():
    policy = RetryPolicy()
    assert policy.is_retryable("POST", err(UpstreamUnavailableError), idempotent=True)  # e.g. idempotency key
    assert not policy.is_retryable("GET", err(UpstreamUnavailableError), idempotent=False)


# ---------------------------------------------------------------------------------------- backoff


def test_retry_after_wins_even_above_max_delay():
    policy = RetryPolicy(max_delay=2.0)
    assert policy.backoff(1, retry_after=5.0) == 5.0
    assert policy.backoff(3, retry_after=0.0) == 0.0


@pytest.mark.parametrize("attempt", [1, 2, 3, 4, 5, 10, 50])
def test_full_jitter_stays_within_the_exponential_cap(attempt):
    policy = RetryPolicy(base_delay=0.5, max_delay=4.0, rng=random.Random(7))
    cap = min(4.0, 0.5 * 2 ** (attempt - 1))
    for _ in range(200):
        assert 0.0 <= policy.backoff(attempt) <= cap


def test_backoff_is_jittered_not_constant():
    # Without jitter, every client that failed together retries together (thundering herd).
    policy = RetryPolicy(base_delay=1.0, max_delay=8.0, rng=random.Random(1))
    assert len({round(policy.backoff(3), 6) for _ in range(50)}) > 10


def test_backoff_is_reproducible_with_a_seeded_rng():
    a = RetryPolicy(rng=random.Random(42))
    b = RetryPolicy(rng=random.Random(42))
    assert [a.backoff(i) for i in range(1, 6)] == [b.backoff(i) for i in range(1, 6)]


# ------------------------------------------------------------------------------ parse_retry_after (given)


def test_parse_retry_after():
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after(format_datetime(now + timedelta(seconds=90), usegmt=True), now=now) == 90.0
    assert parse_retry_after(None) is None and parse_retry_after("soon") is None
