"""T1 acceptance: ServiceClient against the mocks, with fault injection. Needs RetryPolicy (your part)."""

import logging

import httpx
import pytest
from pydantic import SecretStr

from release_copilot.integrations.auth import BearerToken, basic_auth
from release_copilot.integrations.breaker import BreakerState, CircuitBreaker
from release_copilot.integrations.context import CORRELATION_HEADER, correlation
from release_copilot.integrations.errors import (
    AuthError,
    CircuitOpenError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    UpstreamUnavailableError,
)
from release_copilot.integrations.http import ServiceClient
from release_copilot.integrations.retry import RetryPolicy
from tests.integrations_support import MOCK, MOCK_BASE, FakeClock, Mocks

ISSUE = "/rest/api/3/issue/{key}"


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def mocks():
    return Mocks()


def jira(mocks, clock, **kw):
    return ServiceClient(
        "jira", f"{MOCK_BASE}/jira", auth=basic_auth(MOCK.jira_email, SecretStr(MOCK.jira_api_token)),
        transport=mocks.transport(), clock=clock, sleep=clock.sleep, **kw,
    )


def snow(mocks, clock, **kw):
    return ServiceClient(
        "servicenow", f"{MOCK_BASE}/servicenow", auth=basic_auth(MOCK.servicenow_user, SecretStr(MOCK.servicenow_password)),
        transport=mocks.transport(), clock=clock, sleep=clock.sleep, **kw,
    )


# ------------------------------------------------------------------------------------------ retries


async def test_429_with_retry_after_succeeds_on_third_attempt(mocks, clock):
    mocks.fault("/jira", status=429, times=2, retry_after=1)
    async with jira(mocks, clock) as client:
        issue = await client.get_json(ISSUE, path_params={"key": "PAY-102"})
    assert issue["key"] == "PAY-102"
    assert clock.sleeps == [1.0, 1.0]  # waited what the server asked, each time
    assert [c["status"] for c in mocks.calls("/jira")] == [429, 429, 200]


async def test_503_backs_off_with_jitter_within_policy(mocks, clock):
    mocks.fault("/jira", status=503, times=3)
    policy = RetryPolicy(max_attempts=4, base_delay=0.5, max_delay=8.0)
    async with jira(mocks, clock, retry=policy) as client:
        await client.get_json(ISSUE, path_params={"key": "PAY-101"})
    assert len(clock.sleeps) == 3
    assert all(0 <= s <= min(8.0, 0.5 * 2 ** i) for i, s in enumerate(clock.sleeps))


async def test_gives_up_after_max_attempts(mocks, clock):
    mocks.fault("/jira", status=503)
    async with jira(mocks, clock, retry=RetryPolicy(max_attempts=3)) as client:
        with pytest.raises(UpstreamUnavailableError):
            await client.get_json(ISSUE, path_params={"key": "PAY-101"})
    assert len(mocks.calls("/jira")) == 3


async def test_post_is_not_retried_by_default(mocks, clock):
    mocks.fault("/servicenow", status=503, method="POST", times=1)
    async with snow(mocks, clock) as client:
        with pytest.raises(UpstreamUnavailableError):
            await client.post_json("/api/now/table/change_request", json={"short_description": "x"})
    assert len(mocks.calls("/servicenow")) == 1


async def test_post_retries_when_caller_opts_in(mocks, clock):
    mocks.fault("/servicenow", status=503, method="POST", times=1)
    async with snow(mocks, clock) as client:
        body = await client.post_json("/api/now/table/change_request", json={"short_description": "x"}, idempotent=True)
    assert body["result"]["number"].startswith("CHG")


async def test_retry_after_beyond_the_deadline_fails_fast(mocks, clock):
    mocks.fault("/jira", status=429, retry_after=120)
    async with jira(mocks, clock, retry=RetryPolicy(deadline_seconds=30)) as client:
        with pytest.raises(RateLimitedError) as info:
            await client.get_json(ISSUE, path_params={"key": "PAY-101"})
    assert info.value.retry_after == 120 and clock.sleeps == []


# -------------------------------------------------------------------------------------- breaker


async def test_breaker_opens_after_consecutive_failures_and_fails_fast(mocks, clock):
    mocks.fault("/jira", status=503)
    breaker = CircuitBreaker("jira", failure_threshold=5, reset_timeout=30, clock=clock)
    async with jira(mocks, clock, retry=RetryPolicy(max_attempts=1), breaker=breaker) as client:
        for _ in range(5):
            with pytest.raises(UpstreamUnavailableError):
                await client.get_json(ISSUE, path_params={"key": "PAY-101"})
        assert breaker.state is BreakerState.OPEN
        with pytest.raises(CircuitOpenError):
            await client.get_json(ISSUE, path_params={"key": "PAY-101"})
        assert len(mocks.calls("/jira")) == 5  # the 6th call never reached the network

        mocks.state.faults.clear()  # upstream recovers
        clock.now += 31
        assert breaker.state is BreakerState.HALF_OPEN
        assert (await client.get_json(ISSUE, path_params={"key": "PAY-101"}))["key"] == "PAY-101"
        assert breaker.state is BreakerState.CLOSED


async def test_failed_trial_reopens_the_breaker(mocks, clock):
    mocks.fault("/jira", status=503)
    breaker = CircuitBreaker("jira", failure_threshold=2, reset_timeout=10, clock=clock)
    async with jira(mocks, clock, retry=RetryPolicy(max_attempts=1), breaker=breaker) as client:
        for _ in range(2):
            with pytest.raises(UpstreamUnavailableError):
                await client.get_json("/rest/api/3/myself")
        clock.now += 11
        with pytest.raises(UpstreamUnavailableError):  # the one trial call
            await client.get_json("/rest/api/3/myself")
        assert breaker.state is BreakerState.OPEN


async def test_client_errors_do_not_trip_the_breaker(mocks, clock):
    breaker = CircuitBreaker("jira", failure_threshold=2, clock=clock)
    async with jira(mocks, clock, breaker=breaker) as client:
        for _ in range(5):
            with pytest.raises(NotFoundError):
                await client.get_json(ISSUE, path_params={"key": "PAY-999"})
    assert breaker.state is BreakerState.CLOSED


# ------------------------------------------------------------------------------- error taxonomy


async def test_vendor_errors_map_to_types_with_vendor_messages(mocks, clock):
    async with jira(mocks, clock) as client:
        with pytest.raises(NotFoundError, match="Issue does not exist") as info:
            await client.get_json(ISSUE, path_params={"key": "PAY-999"})
    assert (info.value.service, info.value.status, info.value.path) == ("jira", 404, ISSUE)  # template, not the key

    gh = ServiceClient("github", f"{MOCK_BASE}/github", auth=BearerToken(SecretStr("wrong")), transport=mocks.transport())
    async with gh:
        with pytest.raises(AuthError, match="Bad credentials"):
            await gh.get_json("/repos/acme-fin/payments-api")

    async with snow(mocks, clock) as client:
        created = await client.post_json("/api/now/table/change_request", json={"short_description": "x"})
        with pytest.raises(PermissionDeniedError, match="Approval can only be set by an approver"):
            await client.request("PATCH", "/api/now/table/change_request/{id}",
                                 path_params={"id": created["result"]["sys_id"]}, json={"approval": "approved"})
        with pytest.raises(InvalidRequestError, match="risk"):
            await client.post_json("/api/now/table/change_request", json={"short_description": "x", "risk": "9"})


def test_errors_serialise_for_tools():
    err = RateLimitedError("slow down", service="jira", status=429, retry_after=3)
    assert err.to_dict() == {"error_type": "RateLimitedError", "service": "jira", "status": 429, "message": "slow down", "retryable": True}


async def test_transport_errors_are_upstream_unavailable(clock):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = ServiceClient("jira", "http://jira.invalid", transport=httpx.MockTransport(refuse), retry=RetryPolicy(max_attempts=2), clock=clock, sleep=clock.sleep)
    async with client:
        with pytest.raises(UpstreamUnavailableError, match="connection error"):
            await client.get_json("/rest/api/3/myself")
    assert len(clock.sleeps) == 1


# --------------------------------------------------------------------- correlation, paths, logging


async def test_correlation_id_is_sent_and_path_params_are_escaped(clock):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    async with ServiceClient("x", "http://x.test/api/", transport=httpx.MockTransport(handler), clock=clock) as client:
        with correlation("run-123"):
            await client.get_json("/items/{id}", path_params={"id": "../admin x"})
        await client.get_json("/ping")
    assert seen[0].headers[CORRELATION_HEADER] == "run-123"
    assert seen[0].url.raw_path == b"/api/items/..%2Fadmin%20x"  # one segment: cannot escape the route
    assert CORRELATION_HEADER not in seen[1].headers


async def test_logs_have_templates_and_no_secrets(mocks, clock, caplog):
    caplog.set_level(logging.INFO, logger="release_copilot.integrations.http")
    async with jira(mocks, clock) as client:
        await client.get_json(ISSUE, path_params={"key": "PAY-102"}, params={"jql": "text ~ secret-search-term"})
    record = caplog.records[-1]
    assert record.path_template == ISSUE and record.status == 200 and record.attempt == 1
    text = caplog.text
    for leaked in (MOCK.jira_api_token, "PAY-102", "secret-search-term"):
        assert leaked not in text
