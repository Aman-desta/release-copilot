"""Control plane for tests, demos and evals: scenarios, faults, request log, notifications, approvals.

Not part of any real API, and never exposed to agents as a tool.
"""

from dataclasses import asdict
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from release_copilot.mocks import scenarios
from release_copilot.mocks.routers import servicenow
from release_copilot.mocks.state import FaultRule, MockState, current_scenario, get_state

router = APIRouter(prefix="/_admin", tags=["admin"])


class ScenarioIn(BaseModel):
    name: str


class FaultIn(BaseModel):
    path_prefix: str
    kind: Literal["status", "latency", "timeout"] = "status"
    status: int = 503
    delay_ms: int = 0
    times: int | None = None
    method: str | None = None
    scenario: str | None = None
    retry_after: int | None = None


class DecisionIn(BaseModel):
    state: Literal["approved", "rejected"]
    comments: str = ""


class CallbackIn(BaseModel):
    url: str | None


@router.get("/scenarios")
def list_scenarios(state: MockState = Depends(get_state)) -> dict[str, Any]:
    return {
        "active": state.active,
        "scenarios": [{"name": s.name, "description": s.description, "expected": s.expected} for s in scenarios.SCENARIOS.values()],
    }


@router.put("/scenario")
def set_scenario(body: ScenarioIn, state: MockState = Depends(get_state)) -> dict[str, str]:
    if body.name not in scenarios.SCENARIOS:
        raise HTTPException(404, f"Unknown scenario {body.name!r}")
    state.active = body.name
    return {"active": state.active}


@router.post("/reset")
def reset(scenario: str | None = None, state: MockState = Depends(get_state)) -> dict[str, str]:
    """Rebuild one scenario's world (``?scenario=``), or everything: worlds, faults, logs and tokens."""
    state.reset(scenario)
    return {"reset": scenario or "all"}


@router.get("/world")
def world(state: MockState = Depends(get_state)) -> dict[str, Any]:
    """The raw world for the request's scenario: handy when debugging what an agent should have seen."""
    return state.world()


@router.get("/faults")
def list_faults(state: MockState = Depends(get_state)) -> list[dict[str, Any]]:
    return [asdict(f) for f in state.faults]


@router.post("/faults", status_code=201)
def add_fault(body: FaultIn, state: MockState = Depends(get_state)) -> dict[str, Any]:
    return asdict(state.add_fault(FaultRule(**body.model_dump())))


@router.delete("/faults")
def clear_faults(state: MockState = Depends(get_state)) -> dict[str, int]:
    count = len(state.faults)
    state.faults.clear()
    return {"cleared": count}


@router.get("/requests")
def requests(scenario: str | None = None, path_prefix: str = "", state: MockState = Depends(get_state)) -> list[dict[str, Any]]:
    return [r for r in state.requests if (scenario is None or r["scenario"] == scenario) and r["path"].startswith(path_prefix)]


@router.get("/notifications")
def notifications(scenario: str | None = None, state: MockState = Depends(get_state)) -> list[dict[str, Any]]:
    return [n for n in state.notifications if scenario is None or n["scenario"] == scenario]


@router.put("/servicenow/callback")
def set_callback(body: CallbackIn, state: MockState = Depends(get_state)) -> dict[str, Any]:
    """Where to POST approval decisions (in Phase 6, this resumes the interrupted graph)."""
    state.world()["servicenow"]["callback_url"] = body.url
    return {"callback_url": body.url}


@router.post("/servicenow/approvals/{sys_id}")
async def decide(sys_id: str, body: DecisionIn, state: MockState = Depends(get_state)) -> dict[str, Any]:
    """Act as the human approver for one ``sysapproval_approver`` record."""
    world = state.world()
    approval, change = servicenow.decide_approval(world, sys_id, body.state, body.comments)
    event = {
        "event": "approval.decided",
        "scenario": current_scenario.get(),
        "approval_sys_id": approval["sys_id"],
        "change_sys_id": change["sys_id"],
        "change_number": change["number"],
        "approver": approval["approver"],
        "decision": approval["state"],
        "comments": approval["comments"],
        "change_approval": change["approval"],
        "correlation_id": change.get("correlation_id", ""),
    }
    sn = world["servicenow"]
    if sn["callback_url"]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(sn["callback_url"], json=event)
            sn["callback_log"].append({"event": event, "status": resp.status_code})
        except httpx.HTTPError as exc:
            sn["callback_log"].append({"event": event, "error": repr(exc)})
    return {"approval": approval, "change": change, "event": event}
