"""Azure: Entra ID token endpoint + ARM App Service and Azure Monitor metrics (subset).

- Token:  ``POST /azure/login/{tenant}/oauth2/v2.0/token`` (client_credentials, form-encoded)
- ARM:    base URL ``/azure/management``; every call needs ``api-version`` and a Bearer token from above
  - ``GET  …/sites/{site}``, ``…/sites/{site}/slots``, ``…/sites/{site}/slots/{slot}``
  - ``GET  …/sites/{site}/deployments``, ``…/sites/{site}/slots/{slot}/deployments``
  - ``POST …/sites/{site}/slots/{slot}/slotsswap`` and ``…/sites/{site}/slotsswap`` → 202 + ``Azure-AsyncOperation``
  - ``GET  /subscriptions/{sub}/providers/Microsoft.Web/locations/{loc}/operationResults/{id}``
  - ``GET  {resourceId}/providers/Microsoft.Insights/metrics?metricnames=Requests,Http5xx,…``

Health metrics depend on which release a slot runs and on the scenario's ``bad_release``: a bad release
looks fine in staging (low traffic) and fails under production load, starting at the swap time.
"""

import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response

from release_copilot.mocks.auth import azure_auth
from release_copilot.mocks.scenarios.base import RESOURCE_GROUP, SITE, SUBSCRIPTION_ID
from release_copilot.mocks.settings import MockSettings, get_settings
from release_copilot.mocks.state import MockState, get_state, get_world

login = APIRouter(prefix="/azure/login", tags=["azure"])


def _api_version(api_version: str | None = Query(None, alias="api-version")) -> None:
    if not api_version:
        raise HTTPException(
            400, {"error": {"code": "MissingApiVersionParameter", "message": "The api-version query parameter (?api-version=) is required for all requests."}}
        )


arm = APIRouter(prefix="/azure/management", tags=["azure"], dependencies=[Depends(azure_auth), Depends(_api_version)])
SITE_PATH = "/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/sites/{site}"

METRICS = {
    "Requests": ("Requests", "Count", "total"),
    "Http2xx": ("Http 2xx", "Count", "total"),
    "Http4xx": ("Http 4xx", "Count", "total"),
    "Http5xx": ("Http Server Errors", "Count", "total"),
    "HttpResponseTime": ("Response Time", "Seconds", "average"),
}
OPERATIONS: dict[str, str] = {}


@login.post("/{tenant}/oauth2/v2.0/token")
async def token(tenant: str, request: Request, s: MockSettings = Depends(get_settings), state: MockState = Depends(get_state)) -> dict[str, Any]:
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}

    def fail(code: str, desc: str, status: int = 400):
        return HTTPException(status, {"error": code, "error_description": desc})

    if tenant != s.azure_tenant_id:
        raise fail("invalid_tenant", f"AADSTS90002: Tenant '{tenant}' not found.")
    if form.get("grant_type") != "client_credentials":
        raise fail("unsupported_grant_type", "AADSTS70003: The app requested an unsupported grant type.")
    if form.get("client_id") != s.azure_client_id or form.get("client_secret") != s.azure_client_secret:
        raise fail("invalid_client", "AADSTS7000215: Invalid client secret provided.", 401)
    if form.get("scope") != "https://management.azure.com/.default":
        raise fail("invalid_scope", "AADSTS70011: The provided value for scope is not valid.")
    access_token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJub25lIn0." + secrets.token_urlsafe(32)
    state.issue_azure_token(access_token, s.azure_token_ttl_seconds)
    return {"token_type": "Bearer", "expires_in": s.azure_token_ttl_seconds, "ext_expires_in": s.azure_token_ttl_seconds, "access_token": access_token}


def _az(world: dict[str, Any], sub: str, rg: str, site: str) -> dict[str, Any]:
    if sub != SUBSCRIPTION_ID:
        raise HTTPException(404, {"error": {"code": "SubscriptionNotFound", "message": f"The subscription '{sub}' could not be found."}})
    if rg.lower() != RESOURCE_GROUP or site.lower() != SITE:
        raise HTTPException(404, {"error": {"code": "ResourceNotFound", "message": f"The Resource 'Microsoft.Web/sites/{site}' under resource group '{rg}' was not found."}})
    return world["azure"]


def _slot(az: dict[str, Any], slot: str) -> dict[str, Any]:
    if slot not in az["slots"]:
        raise HTTPException(404, {"error": {"code": "ResourceNotFound", "message": f"Slot '{slot}' was not found."}})
    return az["slots"][slot]


@arm.get(SITE_PATH)
def get_site(sub: str, rg: str, site: str, world: dict = Depends(get_world)) -> dict[str, Any]:
    return _az(world, sub, rg, site)["site"]


@arm.get(SITE_PATH + "/slots")
def list_slots(sub: str, rg: str, site: str, world: dict = Depends(get_world)) -> dict[str, Any]:
    return {"value": list(_az(world, sub, rg, site)["slots"].values())}


@arm.get(SITE_PATH + "/slots/{slot}")
def get_slot(sub: str, rg: str, site: str, slot: str, world: dict = Depends(get_world)) -> dict[str, Any]:
    return _slot(_az(world, sub, rg, site), slot)


@arm.get(SITE_PATH + "/deployments")
def site_deployments(sub: str, rg: str, site: str, world: dict = Depends(get_world)) -> dict[str, Any]:
    return {"value": _az(world, sub, rg, site)["deployments"]["production"]}


@arm.get(SITE_PATH + "/slots/{slot}/deployments")
def slot_deployments(sub: str, rg: str, site: str, slot: str, world: dict = Depends(get_world)) -> dict[str, Any]:
    az = _az(world, sub, rg, site)
    _slot(az, slot)
    return {"value": az["deployments"][slot]}


def _swap(az: dict[str, Any], source: str, target: str, response: Response, request: Request) -> None:
    if {source, target} != {"production", "staging"}:
        raise HTTPException(400, {"error": {"code": "BadRequest", "message": "Swap must be between 'staging' and 'production'."}})
    prod, staging = az["site"]["tags"], az["slots"]["staging"]["tags"]
    before = {"production": prod["release"], "staging": staging["release"]}
    prod["release"], staging["release"] = staging["release"], prod["release"]
    az["deployments"]["production"], az["deployments"]["staging"] = az["deployments"]["staging"], az["deployments"]["production"]
    az["swaps"].append({"at": datetime.now(timezone.utc).isoformat(), "before": before, "after": {"production": prod["release"], "staging": staging["release"]}})
    op = str(uuid.uuid4())
    OPERATIONS[op] = "Succeeded"
    response.status_code = 202
    response.headers["Azure-AsyncOperation"] = (
        f"{request.base_url}azure/management/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.Web/locations/eastus2/operationResults/{op}?api-version=2023-12-01"
    )


@arm.post(SITE_PATH + "/slots/{slot}/slotsswap")
def swap_from_slot(
    sub: str, rg: str, site: str, slot: str, request: Request, response: Response,
    payload: dict[str, Any] = Body(...), world: dict = Depends(get_world),
) -> None:
    az = _az(world, sub, rg, site)
    _slot(az, slot)
    _swap(az, slot, payload.get("targetSlot", ""), response, request)


@arm.post(SITE_PATH + "/slotsswap")
def swap_from_production(
    sub: str, rg: str, site: str, request: Request, response: Response,
    payload: dict[str, Any] = Body(...), world: dict = Depends(get_world),
) -> None:
    _swap(_az(world, sub, rg, site), "production", payload.get("targetSlot", ""), response, request)


@arm.get("/subscriptions/{sub}/providers/Microsoft.Web/locations/{loc}/operationResults/{op}")
def operation_result(sub: str, loc: str, op: str) -> dict[str, Any]:
    if op not in OPERATIONS:
        raise HTTPException(404, {"error": {"code": "NotFound", "message": "Operation not found."}})
    return {"id": op, "status": OPERATIONS[op]}


def _minutes(duration: str, default: int) -> int:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", duration or "")
    if not m or not any(m.groups()):
        return default
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def _point(metric: str, i: int, slot: str, healthy: bool) -> float:
    if slot == "staging":
        requests = 30 + i % 7
        return {"Requests": requests, "Http2xx": requests, "Http4xx": i % 2, "Http5xx": 0, "HttpResponseTime": 0.21}[metric]
    requests = 1200 + (i * 37) % 90
    if healthy:
        return {"Requests": requests, "Http2xx": requests - 24, "Http4xx": 20 + i % 5, "Http5xx": (i * 7) % 4, "HttpResponseTime": round(0.18 + (i % 3) * 0.01, 3)}[metric]
    errors = int(requests * 0.12)
    return {"Requests": requests, "Http2xx": requests - errors - 22, "Http4xx": 22, "Http5xx": errors, "HttpResponseTime": round(1.9 + (i % 4) * 0.15, 3)}[metric]


def _metrics(az: dict[str, Any], slot: str, resource_id: str, metricnames: str, timespan: str, interval: str) -> dict[str, Any]:
    names = [n.strip() for n in metricnames.split(",") if n.strip()]
    unknown = [n for n in names if n not in METRICS]
    if unknown:
        raise HTTPException(400, {"error": {"code": "BadRequest", "message": f"Failed to find metric configuration for provider: Microsoft.Web, resource Type: sites, metric: {unknown[0]}, Valid metrics: {', '.join(METRICS)}"}})
    step = _minutes(interval, 5)
    span = _minutes(timespan, 60)
    # Round up so the newest bucket contains "now": a swap shows up in the latest data point immediately.
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    start = end - timedelta(minutes=span)
    current = (az["site"] if slot == "production" else az["slots"][slot])["tags"]["release"]
    last_swap = az["swaps"][-1] if az["swaps"] else None
    swap_at = datetime.fromisoformat(last_swap["at"]) if last_swap else None
    previous = last_swap["before"][slot] if last_swap else current

    series: dict[str, list[dict[str, Any]]] = {n: [] for n in names}
    for i in range(span // step):
        ts = start + timedelta(minutes=i * step)
        release = current if swap_at is None or ts + timedelta(minutes=step) > swap_at else previous
        healthy = release != az["bad_release"]
        for n in names:
            series[n].append({"timeStamp": ts.isoformat().replace("+00:00", "Z"), METRICS[n][2]: float(_point(n, i, slot, healthy))})
    return {
        "cost": 0,
        "timespan": f"{start.isoformat().replace('+00:00', 'Z')}/{end.isoformat().replace('+00:00', 'Z')}",
        "interval": f"PT{step}M",
        "namespace": "Microsoft.Web/sites",
        "resourceregion": "eastus2",
        "value": [
            {
                "id": f"{resource_id}/providers/Microsoft.Insights/metrics/{n}",
                "type": "Microsoft.Insights/metrics",
                "name": {"value": n, "localizedValue": METRICS[n][0]},
                "unit": METRICS[n][1],
                "timeseries": [{"metadatavalues": [], "data": series[n]}],
                "errorCode": "Success",
            }
            for n in names
        ],
    }


for _insights in ("Microsoft.Insights", "microsoft.insights"):

    @arm.get(SITE_PATH + f"/providers/{_insights}/metrics")
    def site_metrics(
        sub: str, rg: str, site: str, metricnames: str = "Requests", timespan: str = "PT1H", interval: str = "PT5M",
        world: dict = Depends(get_world),
    ) -> dict[str, Any]:
        az = _az(world, sub, rg, site)
        return _metrics(az, "production", az["site"]["id"], metricnames, timespan, interval)

    @arm.get(SITE_PATH + f"/slots/{{slot}}/providers/{_insights}/metrics")
    def slot_metrics(
        sub: str, rg: str, site: str, slot: str, metricnames: str = "Requests", timespan: str = "PT1H", interval: str = "PT5M",
        world: dict = Depends(get_world),
    ) -> dict[str, Any]:
        az = _az(world, sub, rg, site)
        return _metrics(az, slot, _slot(az, slot)["id"], metricnames, timespan, interval)
