"""Spec for ``OAuth2ClientCredentials`` (story T1), against the mock Entra ID + ARM. Make these pass."""

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from release_copilot.integrations.auth import OAuth2ClientCredentials
from release_copilot.integrations.errors import AuthError
from release_copilot.integrations.http import ServiceClient
from release_copilot.integrations.retry import RetryPolicy
from tests.integrations_support import MOCK, MOCK_BASE, FakeClock, Mocks

SITE = f"/subscriptions/{'6f1c2a9e-3b4d-4e5f-8a7b-0c1d2e3f4a5b'}/resourceGroups/rg-payments-prod/providers/Microsoft.Web/sites/app-payments-api-prod"
PARAMS = {"api-version": "2023-12-01"}
TOKEN_PATH = "/azure/login"


def make(mocks: Mocks, clock: FakeClock, *, secret: str = MOCK.azure_client_secret, refresh_skew: float = 60.0):
    token_http = httpx.AsyncClient(transport=mocks.transport(), base_url=MOCK_BASE)
    auth = OAuth2ClientCredentials(
        token_url=f"{MOCK_BASE}/azure/login/{MOCK.azure_tenant_id}/oauth2/v2.0/token",
        client_id=MOCK.azure_client_id,
        client_secret=SecretStr(secret),
        scope="https://management.azure.com/.default",
        http=token_http,
        refresh_skew=refresh_skew,
        clock=clock,
    )
    client = ServiceClient("azure", f"{MOCK_BASE}/azure/management", auth=auth, transport=mocks.transport(),
                           retry=RetryPolicy(max_attempts=1), clock=clock, sleep=clock.sleep)
    return auth, client


@pytest.fixture
def clock():
    return FakeClock()


async def test_token_is_fetched_once_and_reused(clock):
    mocks = Mocks()
    auth, client = make(mocks, clock)
    async with client:
        for _ in range(3):
            assert (await client.get_json(SITE, params=PARAMS))["name"] == "app-payments-api-prod"
    assert auth.token_requests == 1
    assert len(mocks.calls(TOKEN_PATH)) == 1


async def test_concurrent_requests_share_one_token_request(clock):
    mocks = Mocks()
    auth, client = make(mocks, clock)
    async with client:
        await asyncio.gather(*(client.get_json(SITE, params=PARAMS) for _ in range(10)))
    assert auth.token_requests == 1


async def test_refreshes_before_expiry(clock):
    mocks = Mocks(azure_token_ttl_seconds=3600)
    auth, client = make(mocks, clock, refresh_skew=60)
    async with client:
        await client.get_json(SITE, params=PARAMS)
        clock.now += 3600 - 61  # 61s left: still valid
        await client.get_json(SITE, params=PARAMS)
        assert auth.token_requests == 1
        clock.now += 2  # 59s left: inside the skew window -> refresh proactively
        await client.get_json(SITE, params=PARAMS)
    assert auth.token_requests == 2


async def test_skew_never_exceeds_half_the_token_lifetime(clock):
    # A 10s token with a 60s skew would otherwise be "expiring" on every call.
    mocks = Mocks(azure_token_ttl_seconds=10)
    auth, client = make(mocks, clock, refresh_skew=60)
    async with client:
        await client.get_json(SITE, params=PARAMS)
        clock.now += 4
        await client.get_json(SITE, params=PARAMS)
        assert auth.token_requests == 1
        clock.now += 2  # 6s elapsed: past half the lifetime -> refresh
        await client.get_json(SITE, params=PARAMS)
    assert auth.token_requests == 2


async def test_revoked_token_is_refreshed_once_and_the_request_retried(clock):
    mocks = Mocks()
    auth, client = make(mocks, clock)
    async with client:
        await client.get_json(SITE, params=PARAMS)
        mocks.state.azure_tokens.clear()  # server-side revocation: our cached token is now rejected with 401
        assert (await client.get_json(SITE, params=PARAMS))["name"] == "app-payments-api-prod"
    assert auth.token_requests == 2


async def test_persistent_401_is_not_retried_forever(clock):
    mocks = Mocks()
    auth, client = make(mocks, clock)
    mocks.fault("/azure/management", status=401)  # every API call answers 401
    async with client:
        with pytest.raises(AuthError):
            await client.get_json(SITE, params=PARAMS)
    assert auth.token_requests == 2  # first token + exactly one refresh
    assert len(mocks.calls("/azure/management")) == 2


async def test_bad_client_secret_raises_auth_error_with_vendor_message(clock):
    mocks = Mocks()
    _, client = make(mocks, clock, secret="wrong")
    async with client:
        with pytest.raises(AuthError, match="invalid_client"):
            await client.get_json(SITE, params=PARAMS)
    assert not mocks.calls("/azure/management")  # no API call without a token


def test_secret_is_never_in_repr(clock):
    auth, _ = make(Mocks(), clock, secret="super-secret-value")
    assert "super-secret-value" not in repr(auth)


async def test_token_endpoint_outage_is_transient_and_retried(clock):
    # Entra ID blips (503) are upstream trouble, not bad credentials: retry them like any other call.
    mocks = Mocks()
    mocks.fault("/azure/login", status=503, times=1)
    auth, client = make(mocks, clock)
    client.retry = RetryPolicy(max_attempts=3)
    async with client:
        assert (await client.get_json(SITE, params=PARAMS))["name"] == "app-payments-api-prod"
    assert auth.token_requests == 2 and len(clock.sleeps) == 1


async def test_token_endpoint_rate_limit_is_retryable(clock):
    from release_copilot.integrations.errors import RateLimitedError

    mocks = Mocks()
    mocks.fault("/azure/login", status=429, retry_after=2)
    _, client = make(mocks, clock)
    async with client:
        with pytest.raises(RateLimitedError):
            await client.get_json(SITE, params=PARAMS)