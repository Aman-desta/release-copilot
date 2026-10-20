"""Middleware: pick the scenario, inject faults, and log every request (the agent's "trajectory")."""

import asyncio
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from release_copilot.mocks.settings import get_settings
from release_copilot.mocks.state import FaultRule, MockState, current_scenario

SCENARIO_HEADER = "x-mock-scenario"
FAULT_HEADER = "x-mock-fault"


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def mock_middleware(request: Request, call_next):
        state: MockState = request.app.state.mock
        path = request.url.path
        scenario = request.headers.get(SCENARIO_HEADER) or state.active
        token = current_scenario.set(scenario)
        started = time.perf_counter()
        try:
            if path.startswith("/_admin") or path in {"/", "/docs", "/openapi.json"}:
                return await call_next(request)

            fault = _header_fault(request.headers.get(FAULT_HEADER)) or state.match_fault(request.method, path, scenario)
            response = await _apply_fault(fault) if fault else None
            if response is None:
                response = await call_next(request)

            state.log_request(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "scenario": scenario,
                    "method": request.method,
                    "path": path,
                    "query": str(request.url.query),
                    "status": response.status_code,
                    "fault": fault.kind if fault else None,
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
            return response
        finally:
            current_scenario.reset(token)


def _header_fault(value: str | None) -> FaultRule | None:
    """``X-Mock-Fault: 503`` | ``429`` | ``timeout`` | ``latency=1500``: a one-off fault for this request only."""
    if not value:
        return None
    if value == "timeout":
        return FaultRule(path_prefix="", kind="timeout")
    if value.startswith("latency="):
        return FaultRule(path_prefix="", kind="latency", delay_ms=int(value.split("=", 1)[1]))
    return FaultRule(path_prefix="", kind="status", status=int(value), retry_after=1 if value == "429" else None)


async def _apply_fault(rule: FaultRule) -> Response | None:
    """Return an error response, or None to let the request through (after any added latency)."""
    if rule.kind == "latency":
        await asyncio.sleep(rule.delay_ms / 1000)
        return None
    if rule.kind == "timeout":
        await asyncio.sleep(get_settings().timeout_fault_seconds)
        return JSONResponse({"error": "mock timeout"}, status_code=504)
    headers = {"Retry-After": str(rule.retry_after)} if rule.retry_after is not None else {}
    return JSONResponse({"error": f"mock fault {rule.status}"}, status_code=rule.status, headers=headers)
