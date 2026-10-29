"""Azure read tools: what runs where (slots, deployments) and how healthy it is (metrics)."""

import asyncio
from typing import TYPE_CHECKING, Literal

from langchain_core.tools import BaseTool
from pydantic import Field

from release_copilot.integrations.models import error_rate
from release_copilot.tools.base import Evidence, ToolArgs, ToolPayload, View, make_tool

if TYPE_CHECKING:
    from release_copilot.tools.toolbox import Toolbox

Slot = Literal["production", "staging"]


class SlotsArgs(ToolArgs):
    pass


class DeploymentView(View):
    id: str
    status: str
    finished: str | None = None
    message: str


class SlotView(View):
    id: str
    slot: str
    release: str | None = None
    state: str
    active_deployment: DeploymentView | None = None
    previous_deployments: list[DeploymentView] = []


class SlotsView(View):
    site: str
    slots: list[SlotView]


async def _slots(box: "Toolbox", args: SlotsArgs) -> ToolPayload:
    slots = await box.azure.list_slots()
    histories = await asyncio.gather(*(box.azure.deployments(s.slot) for s in slots))
    views = []
    for slot, deployments in zip(slots, histories):
        as_view = [
            DeploymentView(id=d.evidence_id, status=d.status, finished=d.ended.isoformat() if d.ended else None, message=d.message)
            for d in deployments
        ]
        active = next((v for v, d in zip(as_view, deployments) if d.active), None)
        views.append(
            SlotView(
                id=slot.evidence_id, slot=slot.slot, release=slot.release, state=slot.state,
                active_deployment=active, previous_deployments=[v for v in as_view if v is not active][:3],
            )
        )
    return ToolPayload(
        view=SlotsView(site=box.azure.site, slots=views),
        evidence=[Evidence(s.evidence_id) for s in slots] + [Evidence(d.evidence_id) for ds in histories for d in ds],
        objects=[*slots, *(d for ds in histories for d in ds)],
    )


class HealthArgs(ToolArgs):
    slot: Slot = Field(default="production", description="Which slot to check.")
    minutes: int = Field(default=60, ge=5, le=1440, description="Look-back window in minutes (5-1440).")


class HealthView(View):
    slot: str
    window_minutes: int
    error_rate_latest: float | None = None  # share of requests with 5xx in the latest interval
    error_rate_window: float | None = None
    requests_latest: float | None = None
    response_time_latest_s: float | None = None
    metric_ids: list[str]


async def _health(box: "Toolbox", args: HealthArgs) -> ToolPayload:
    metrics = await box.azure.metrics(["Requests", "Http5xx", "HttpResponseTime"], slot=args.slot, timespan=f"PT{args.minutes}M")
    requests, errors, latency = metrics["Requests"], metrics["Http5xx"], metrics["HttpResponseTime"]
    rate_latest = error_rate(errors, requests)
    rate_window = error_rate(errors, requests, last=len(requests.points) or 1)
    view = HealthView(
        slot=args.slot,
        window_minutes=args.minutes,
        error_rate_latest=round(rate_latest, 4) if rate_latest is not None else None,
        error_rate_window=round(rate_window, 4) if rate_window is not None else None,
        requests_latest=requests.latest,
        response_time_latest_s=latency.latest,
        metric_ids=[m.evidence_id for m in metrics.values()],
    )
    return ToolPayload(view=view, evidence=[Evidence(m.evidence_id) for m in metrics.values()], objects=list(metrics.values()))


def build(box: "Toolbox") -> list[BaseTool]:
    return [
        make_tool(
            lambda a: _slots(box, a),
            name="azure_slots",
            description=(
                "Show which release runs in each App Service slot (production, staging), with the active deployment "
                "and recent deployment history. Use it to know what a swap would change and what a rollback restores."
            ),
            args_schema=SlotsArgs, access="read", domain="azure",
        ),
        make_tool(
            lambda a: _health(box, a),
            name="azure_health",
            description=(
                "Read a slot's live health from Azure Monitor: 5xx error rate (latest interval and whole window), "
                "request volume and response time. Staging has little traffic, so its numbers are weak signals."
            ),
            args_schema=HealthArgs, access="read", domain="azure",
        ),
    ]
