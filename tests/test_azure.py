"""T3: AzureAppServiceClient against the mock ARM. Read tests are provided; metrics and swap are the spec."""

import logging

import httpx
import pytest

from release_copilot.config import get_settings
from release_copilot.integrations.azure import METRICS_API_VERSION
from release_copilot.integrations.clients import azure_client
from release_copilot.integrations.errors import (
    ConflictError,
    IntegrationError,
    InvalidRequestError,
    OperationFailedError,
    UpstreamUnavailableError,
)
from release_copilot.integrations.models import error_rate
from tests.integrations_support import FakeClock, Mocks

SUBSCRIPTION = "6f1c2a9e-3b4d-4e5f-8a7b-0c1d2e3f4a5b"
HEALTH = ["Requests", "Http5xx", "HttpResponseTime"]


def make(mocks: Mocks, clock: FakeClock, transport: httpx.AsyncBaseTransport | None = None):
    client = azure_client(get_settings(), transport=transport or mocks.transport())
    client._sleep, client._clock = clock.sleep, clock
    return client


@pytest.fixture
def clock():
    return FakeClock()


def swap_posts(mocks: Mocks) -> list[dict]:
    return [c for c in mocks.calls("/azure/management") if c["method"] == "POST"]


# ------------------------------------------------------------------------------------- reads (given)


async def test_slots_and_releases(mock_env, clock):
    async with make(Mocks(), clock) as az:
        slots = {s.slot: s for s in await az.list_slots()}
    assert slots["production"].release == "2.3.1" and slots["staging"].release == "2.4.0-rc.1"
    assert slots["staging"].state == "Running" and slots["staging"].evidence_id == "azure:slot:staging"


async def test_deployment_history(mock_env, clock):
    async with make(Mocks(), clock) as az:
        prod = await az.deployments("production")
        staging = await az.deployments("staging")
    assert (prod[0].id, prod[0].status, prod[0].active) == ("dep-2.3.1", "success", True)
    assert staging[0].id == "dep-2.4.0-rc.1" and staging[0].evidence_id == "azure:deploy:staging:dep-2.4.0-rc.1"


async def test_logs_never_contain_the_subscription_id(mock_env, clock, caplog):
    caplog.set_level(logging.INFO, logger="release_copilot.integrations")
    async with make(Mocks(), clock) as az:
        await az.list_slots()
        await az.deployments("staging")
    assert caplog.records and SUBSCRIPTION not in caplog.text


# ------------------------------------------------------------------------------------- metrics (spec)


async def test_metrics_are_typed_series(mock_env, clock):
    mocks = Mocks()
    async with make(mocks, clock) as az:
        m = await az.metrics(HEALTH, slot="production")
    assert set(m) == set(HEALTH)
    assert len(m["Requests"].points) == 12 and m["Requests"].interval == "PT5M"
    assert (m["Http5xx"].aggregation, m["Http5xx"].unit) == ("total", "Count")
    assert m["HttpResponseTime"].aggregation == "average" and m["HttpResponseTime"].latest < 0.5
    assert error_rate(m["Http5xx"], m["Requests"]) < 0.01
    assert m["Http5xx"].evidence_id == "azure:metric:production:Http5xx"
    query = mocks.calls("/azure/management")[-1]["query"]
    assert f"api-version={METRICS_API_VERSION}" in query and "aggregation=" in query


async def test_slot_metrics(mock_env, clock):
    async with make(Mocks(), clock) as az:
        m = await az.metrics(["Requests"], slot="staging")
    assert m["Requests"].slot == "staging" and m["Requests"].latest < 100  # staging gets little traffic


async def test_unknown_metric(mock_env, clock):
    async with make(Mocks(), clock) as az:
        with pytest.raises(InvalidRequestError, match="metric"):
            await az.metrics(["Nope"])


# ---------------------------------------------------------------------------------------- swap (spec)


async def test_swap_polls_the_long_running_operation(mock_env, clock):
    mocks = Mocks()
    async with make(mocks, clock) as az:
        result = await az.swap(release="2.4.0-rc.1")
        assert (await az.get_slot("production")).release == "2.4.0-rc.1"
    assert result.status == "Succeeded" and result.polls == 3
    assert (result.target_release_before, result.target_release_after) == ("2.3.1", "2.4.0-rc.1")
    assert clock.sleeps == [1.0, 1.0, 1.0]  # Retry-After from the POST, then from each InProgress poll
    assert len(swap_posts(mocks)) == 1


async def test_swap_is_idempotent(mock_env, clock):
    mocks = Mocks()
    async with make(mocks, clock) as az:
        await az.swap(release="2.4.0-rc.1")
        again = await az.swap(release="2.4.0-rc.1")
    assert again.status == "AlreadyInPlace" and again.operation_id is None
    assert len(swap_posts(mocks)) == 1  # a second POST would have swapped back


async def test_swap_refuses_when_the_source_has_another_release(mock_env, clock):
    mocks = Mocks()
    async with make(mocks, clock) as az:
        with pytest.raises(ConflictError, match="9.9.9"):
            await az.swap(release="9.9.9")
    assert swap_posts(mocks) == []


async def test_failed_deploy_is_detected_and_rolled_back(mock_env, clock):
    """T3 acceptance: after the swap production 5xx > 10%; swapping back recovers."""
    mocks = Mocks("failed_deploy")
    async with make(mocks, clock) as az:
        before = await az.metrics(HEALTH)
        assert error_rate(before["Http5xx"], before["Requests"]) < 0.01

        await az.swap(release="2.4.0-rc.1")
        after = await az.metrics(HEALTH)
        assert error_rate(after["Http5xx"], after["Requests"]) > 0.10
        assert after["HttpResponseTime"].latest > 1.5

        rollback = await az.swap(release="2.3.1")  # rollback is the same idempotent call
        recovered = await az.metrics(HEALTH)
    assert rollback.status == "Succeeded"
    assert error_rate(recovered["Http5xx"], recovered["Requests"]) < 0.01
    assert len(swap_posts(mocks)) == 2


async def test_failed_operation_raises_and_changes_nothing(mock_env, clock):
    mocks = Mocks()
    mocks.state.world()["azure"]["swap_fails"] = True
    async with make(mocks, clock) as az:
        with pytest.raises(OperationFailedError, match="Warm-up"):
            await az.swap(release="2.4.0-rc.1")
        assert (await az.get_slot("production")).release == "2.3.1"


async def test_timeout_reports_unknown_outcome(mock_env, clock):
    mocks = Mocks()
    mocks.state.world()["azure"]["swap_polls"] = 1000
    async with make(mocks, clock) as az:
        with pytest.raises(UpstreamUnavailableError, match="unknown"):
            await az.swap(release="2.4.0-rc.1", timeout=10)
    assert sum(clock.sleeps) <= 11
    assert len(swap_posts(mocks)) == 1  # never re-POSTed


class ForeignOperationUrl(httpx.AsyncBaseTransport):
    """Rewrites the Azure-AsyncOperation header to point at another host."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if "azure-asyncoperation" in response.headers:
            await response.aread()
            headers = dict(response.headers)
            headers["azure-asyncoperation"] = "https://attacker.example/operationResults/x?api-version=1"
            return httpx.Response(response.status_code, headers=headers, content=response.content, request=request)
        return response


async def test_operation_url_on_another_host_is_never_polled(mock_env, clock):
    mocks = Mocks()
    async with make(mocks, clock, transport=ForeignOperationUrl(mocks.transport())) as az:
        with pytest.raises(IntegrationError, match="attacker.example"):
            await az.swap(release="2.4.0-rc.1")
    assert not [c for c in mocks.calls() if "operationResults" in c["path"]]


async def test_polls_are_logged_as_a_template(mock_env, clock, caplog):
    caplog.set_level(logging.INFO, logger="release_copilot.integrations")
    async with make(Mocks(), clock) as az:
        await az.swap(release="2.4.0-rc.1")
    assert "operationResults" not in caplog.text and SUBSCRIPTION not in caplog.text


# ------------------------------------------------------------------------------- unknown outcomes (review)


async def test_poll_failures_after_the_post_mean_outcome_unknown(mock_env, clock):
    mocks = Mocks()
    operations = f"/azure/management/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Web/locations"
    mocks.fault(operations, status=503)  # every poll fails, even after ServiceClient's retries
    async with make(mocks, clock) as az:
        with pytest.raises(UpstreamUnavailableError, match="outcome is unknown") as info:
            await az.swap(release="2.4.0-rc.1")
    assert isinstance(info.value.__cause__, UpstreamUnavailableError)  # the underlying reason is kept
    assert len(swap_posts(mocks)) == 1  # and nothing was re-sent


async def test_ambiguous_post_failure_is_outcome_unknown_but_4xx_is_not(mock_env, clock):
    mocks = Mocks()
    async with make(mocks, clock) as az:
        mocks.fault("/azure/management", status=504, method="POST", times=1)
        with pytest.raises(UpstreamUnavailableError, match="outcome is unknown"):
            await az.swap(release="2.4.0-rc.1")
        mocks.fault("/azure/management", status=409, method="POST", times=1)  # e.g. another swap in progress
        with pytest.raises(ConflictError):
            await az.swap(release="2.4.0-rc.1")


async def test_operation_id_never_contains_the_subscription(mock_env, clock):
    async with make(Mocks(), clock) as az:
        result = await az.swap(release="2.4.0-rc.1")
    assert result.operation_id and "/" not in result.operation_id and SUBSCRIPTION not in result.operation_id
