"""Azure App Service (ARM) and Azure Monitor client: slot state, deployment history, health metrics, slot swap.

Every call goes through path *templates* (subscription, resource group and site are path params), so logs and
errors never contain subscription ids.

Swaps are written as "ensure the target runs release X" (idempotent), and every ambiguous exit after the swap
request is sent is reported as "outcome unknown", never as an ordinary failure a caller might retry blindly.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from release_copilot.integrations.errors import (
    ConflictError,
    IntegrationError,
    OperationFailedError,
    UpstreamUnavailableError,
    vendor_message,
)
from release_copilot.integrations.http import ServiceClient
from release_copilot.integrations.models import KUDU_STATUS, AppServiceSlot, Deployment, MetricPoint, MetricSeries, SwapResult
from release_copilot.integrations.retry import parse_retry_after

WEB_API_VERSION = "2023-12-01"  # Microsoft.Web
METRICS_API_VERSION = "2023-10-01"  # Microsoft.Insights/metrics
SITE = "/subscriptions/{subscription}/resourceGroups/{resource_group}/providers/Microsoft.Web/sites/{site}"
SLOT = SITE + "/slots/{slot}"
OPERATION_LOG_PATH = "{azure-async-operation}"  # what polls are logged as


class AzureAppServiceClient:
    def __init__(
        self,
        http: ServiceClient,
        *,
        subscription_id: str,
        resource_group: str,
        site: str,
        poll_interval: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._ids = {"subscription": subscription_id, "resource_group": resource_group, "site": site}
        self.site = site
        self.poll_interval = poll_interval  # used when the service sends no Retry-After
        self._sleep = sleep
        self._clock = clock
        self._closers: list[Callable[[], Awaitable[None]]] = [http.aclose]

    def on_close(self, closer: Callable[[], Awaitable[None]]) -> None:
        """Register extra resources to close with the client (the token endpoint's HTTP client)."""
        self._closers.append(closer)

    async def aclose(self) -> None:
        for closer in self._closers:
            await closer()

    async def __aenter__(self) -> "AzureAppServiceClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------------------------- helpers

    def _resource(self, slot: str) -> tuple[str, dict[str, str]]:
        """Path template and params for the production site (``slot="production"``) or a named slot."""
        if slot == "production":
            return SITE, dict(self._ids)
        return SLOT, {**self._ids, "slot": slot}

    async def _get(self, path: str, path_params: dict[str, str], params: dict[str, Any] | None = None, api_version: str = WEB_API_VERSION) -> Any:
        return await self._http.get_json(path, path_params=path_params, params={"api-version": api_version, **(params or {})})

    def _slot(self, raw: dict[str, Any], slot: str) -> AppServiceSlot:
        props = raw.get("properties", {})
        return AppServiceSlot(
            site=self.site,
            slot=slot,
            state=props.get("state", "Unknown"),
            host_name=props.get("defaultHostName", ""),
            release=(raw.get("tags") or {}).get("release"),
        )

    # ------------------------------------------------------------------------------------ read API

    async def get_slot(self, slot: str = "production") -> AppServiceSlot:
        path, params = self._resource(slot)
        return self._slot(await self._get(path, params), slot)

    async def list_slots(self) -> list[AppServiceSlot]:
        """The production site followed by every deployment slot."""
        production = await self.get_slot("production")
        raw = await self._get(SITE + "/slots", dict(self._ids))
        return [production, *(self._slot(s, s["name"].split("/")[-1]) for s in raw.get("value", []))]

    async def deployments(self, slot: str = "production") -> list[Deployment]:
        path, params = self._resource(slot)
        raw = await self._get(path + "/deployments", params)
        return [
            Deployment(
                id=d["name"],
                slot=slot,
                status=KUDU_STATUS.get(d["properties"].get("status"), "unknown"),  # type: ignore[arg-type]
                active=bool(d["properties"].get("active")),
                message=d["properties"].get("message", ""),
                author=d["properties"].get("author", ""),
                deployer=d["properties"].get("deployer", ""),
                started=d["properties"].get("start_time"),
                ended=d["properties"].get("end_time"),
            )
            for d in raw.get("value", [])
        ]

    async def metrics(
        self, names: Sequence[str], *, slot: str = "production", timespan: str = "PT1H", interval: str = "PT5M"
    ) -> dict[str, MetricSeries]:
        """Azure Monitor metrics for the site or a slot, keyed by metric name.

        ``GET {resource}/providers/Microsoft.Insights/metrics`` with ``metricnames`` (comma-joined), ``timespan``,
        ``interval``, ``aggregation=Total,Average`` and ``api-version=METRICS_API_VERSION``.
        Each data point carries one aggregation key (``total``, ``average``, ...): use the first one present and
        report it as ``MetricSeries.aggregation``. A metric with no timeseries gives an empty ``points``.
        """
        path, path_params = self._resource(slot)
        raw = await self._get(
            path + "/providers/Microsoft.Insights/metrics",
            path_params,
            {
                "metricnames": ",".join(names),
                "timespan": timespan,
                "interval": interval,
                "aggregation": "Total,Average",
            },
            api_version=METRICS_API_VERSION,
        )
        result: dict[str, MetricSeries] = {}
        for metric in raw.get("value", []):
            name = metric["name"]["value"]
            timeseries = metric.get("timeseries", [])
            data = timeseries[0].get("data", []) if timeseries else []
            aggregation = next((key for point in data for key in ("total", "average") if key in point), "total")
            points = tuple(
                MetricPoint(timestamp=point["timeStamp"], value=point.get(aggregation))
                for point in data
            )
            result[name] = MetricSeries(
                name=name,
                slot=slot,
                unit=metric["unit"],
                aggregation=aggregation,
                interval=raw.get("interval", interval),
                points=points,
            )
        return result

    # ----------------------------------------------------------------------------------- write API

    async def swap(self, *, release: str, source: str = "staging", target: str = "production", timeout: float = 600.0) -> SwapResult:
        """Make ``target`` run ``release`` by swapping it in from ``source``. Idempotent; rollback is the same call.

        A swap is a *toggle*: sending it twice swaps back. So this is written as "ensure target runs release":
        1. Read both slots. Target already runs ``release`` → ``AlreadyInPlace``, no POST. Source does not run
           ``release`` → ``ConflictError`` (the world is not what the caller believes; don't guess).
        2. POST ``…/slots/{source}/slotsswap`` (``…/slotsswap`` when source is production) with
           ``{"targetSlot": target, "preserveVnet": true}``. Expect 202 and an ``Azure-AsyncOperation`` URL.
        3. Poll that URL until the status is final. Only if it is on the same origin as the ARM base URL
           (otherwise raise IntegrationError: never send the bearer token to another host). Log polls as
           ``OPERATION_LOG_PATH``. Wait ``Retry-After`` (or ``poll_interval``) between polls with ``self._sleep``.
           Past ``timeout`` → ``UpstreamUnavailableError`` saying the outcome is unknown (callers re-read state,
           and must never re-POST blindly).
        4. ``Succeeded`` → re-read target and confirm it runs ``release`` (else ``ConflictError``); return the
           result with ``polls``. ``Failed``/``Canceled`` → ``OperationFailedError`` with the operation's error message.
        """
        started = self._clock()
        target_before = await self.get_slot(target)
        if target_before.release == release:
            return SwapResult(
                source=source,
                target=target,
                status="AlreadyInPlace",
                target_release_before=target_before.release,
                target_release_after=target_before.release,
            )

        source_slot = await self.get_slot(source)
        if source_slot.release != release:
            raise ConflictError(
                f"Source slot {source!r} runs release {source_slot.release!r}, not requested release {release!r}.",
                service=self._http.service,
                method="POST",
                path=self._resource(source)[0] + "/slotsswap",
            )

        swap_path, path_params = self._resource(source)
        post_path = swap_path + "/slotsswap"
        try:
            response = await self._http.request(
                "POST",
                post_path,
                path_params=path_params,
                params={"api-version": WEB_API_VERSION},
                json={"targetSlot": target, "preserveVnet": True},
                idempotent=False,
                ok=(202,),
            )
        except UpstreamUnavailableError as exc:
            # Timeout or 5xx: Azure may have accepted the swap anyway. (4xx and 429 are definite: nothing started.)
            raise self._unknown_outcome(f"the swap request failed ({exc.message})", path=post_path) from exc

        # From here on Azure has accepted the swap: any exit other than a final status is "outcome unknown".
        try:
            operation_url = self._operation_url(response, post_path)
            operation, polls = await self._wait_for_operation(operation_url, response, started, timeout)
        except OperationFailedError:
            raise
        except IntegrationError as exc:
            raise self._unknown_outcome(exc.message, path=OPERATION_LOG_PATH) from exc

        target_after = await self.get_slot(target)
        if target_after.release != release:
            raise ConflictError(
                f"Swap succeeded, but target slot {target!r} runs release {target_after.release!r}, not requested release {release!r}.",
                service=self._http.service,
                method="GET",
                path=self._resource(target)[0],
            )
        return SwapResult(
            source=source,
            target=target,
            status="Succeeded",
            target_release_before=target_before.release,
            target_release_after=target_after.release,
            # Always the URL's last segment: ARM's "id" field is a resource path that embeds the subscription id.
            operation_id=operation_url.path.rstrip("/").rsplit("/", 1)[-1],
            polls=polls,
        )

    def _unknown_outcome(self, reason: str, *, path: str) -> UpstreamUnavailableError:
        return UpstreamUnavailableError(
            f"Azure swap outcome is unknown: {reason}. Re-read slot state before acting; calling swap() again is safe.",
            service=self._http.service,
            method="POST",
            path=path,
        )

    def _operation_url(self, response: httpx.Response, post_path: str) -> httpx.URL:
        header = response.headers.get("Azure-AsyncOperation")
        if not header:
            raise IntegrationError("the swap response had no Azure-AsyncOperation URL", service=self._http.service, path=post_path)
        try:
            url = self._http.base_url.join(header)
        except httpx.InvalidURL as exc:
            raise IntegrationError("Azure returned an invalid Azure-AsyncOperation URL", service=self._http.service, path=post_path) from exc
        base = self._http.base_url
        if (url.scheme, url.host, url.port) != (base.scheme, base.host, base.port):
            # Never send the bearer token to another host.
            raise IntegrationError(
                f"Azure-AsyncOperation URL must use the ARM host; received {url.host!r}", service=self._http.service, path=post_path
            )
        return url

    async def _wait_for_operation(
        self, operation_url: httpx.URL, response: httpx.Response, started: float, timeout: float
    ) -> tuple[dict[str, Any], int]:
        """Poll until the operation is final. Returns (operation body, polls) on Succeeded."""
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        polls = 0
        while True:
            remaining = timeout - (self._clock() - started)
            if remaining > 0:
                wait = self.poll_interval if retry_after is None else retry_after
                await self._sleep(min(wait, remaining))
            if self._clock() - started >= timeout:
                raise IntegrationError(f"no final status after {timeout:g} seconds", service=self._http.service, path=OPERATION_LOG_PATH)

            poll_response = await self._http.request("GET", str(operation_url), log_path=OPERATION_LOG_PATH)
            polls += 1
            operation = poll_response.json()
            status = operation.get("status")
            if status == "Succeeded":
                return operation, polls
            if status in {"Failed", "Canceled"}:
                message = vendor_message(operation) or f"Azure swap operation ended with status {status}"
                raise OperationFailedError(message, service=self._http.service, method="GET", path=OPERATION_LOG_PATH, status=poll_response.status_code)
            if status != "InProgress":
                raise IntegrationError(f"unexpected operation status {status!r}", service=self._http.service, path=OPERATION_LOG_PATH)
            retry_after = parse_retry_after(poll_response.headers.get("Retry-After"))
