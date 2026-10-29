"""ServiceNow read tool: the service's CMDB profile and its change history (read-only; writes are workflow steps)."""

from typing import TYPE_CHECKING, Annotated

from langchain_core.tools import BaseTool
from pydantic import Field

from release_copilot.integrations.servicenow import Q
from release_copilot.tools.base import Evidence, ToolArgs, ToolPayload, View, make_tool

if TYPE_CHECKING:
    from release_copilot.tools.toolbox import Toolbox

ServiceName = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9\-]*$", examples=["payments-api"])]


class ProfileArgs(ToolArgs):
    service: ServiceName = Field(description="Service name as registered in the CMDB, e.g. payments-api.")
    recent_changes: int = Field(default=5, ge=1, le=20, description="How many recent change requests to include.")


class ChangeView(View):
    id: str
    summary: str
    state: str
    risk: str
    outcome: str | None = None  # close code: successful | unsuccessful | ...
    notes: str | None = None
    planned_start: str | None = None


class ProfileView(View):
    id: str | None = None
    service: str
    found: bool
    business_criticality: str | None = None
    support_group: str | None = None
    recent_changes: list[ChangeView] = []


async def _profile(box: "Toolbox", args: ProfileArgs) -> ToolPayload:
    ci = await box.servicenow.get_ci(args.service)
    if ci is None:
        return ToolPayload(view=ProfileView(service=args.service, found=False))
    group = await box.servicenow.get_group_by_id(ci.support_group_sys_id) if ci.support_group_sys_id else None
    changes = (await box.servicenow.find_changes(Q().eq("cmdb_ci", ci.sys_id).order_by_desc("sys_created_on")))[: args.recent_changes]
    view = ProfileView(
        id=ci.evidence_id,
        service=ci.name,
        found=True,
        business_criticality=ci.business_criticality or None,
        support_group=group.name if group else None,
        recent_changes=[
            ChangeView(
                id=c.evidence_id, summary=c.short_description, state=c.state_label, risk=c.risk_label,
                outcome=c.close_code or None, notes=c.close_notes or None,
                planned_start=c.start_date.isoformat() if c.start_date else None,
            )
            for c in changes
        ],
    )
    return ToolPayload(view=view, evidence=[Evidence(ci.evidence_id), *(Evidence(c.evidence_id) for c in changes)], objects=[ci, *changes])


def build(box: "Toolbox") -> list[BaseTool]:
    return [
        make_tool(
            lambda a: _profile(box, a),
            name="servicenow_service_profile",
            description=(
                "Look up a service in the CMDB (business criticality, support group) and its recent change requests "
                "with their outcomes and close notes. Past unsuccessful changes explain known risks."
            ),
            args_schema=ProfileArgs, access="read", domain="servicenow",
        )
    ]
