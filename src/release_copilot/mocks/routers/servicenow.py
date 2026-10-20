"""ServiceNow Table API (subset). Base URL: ``/servicenow``.

- ``GET  /api/now/table/{table}`` with ``sysparm_query`` (``=``, ``!=``, ``LIKE``, ``IN``, ``STARTSWITH``, ``^`` = AND,
  ``ORDERBY``/``ORDERBYDESC``), ``sysparm_limit``, ``sysparm_offset``, ``sysparm_fields``, ``sysparm_display_value``
- ``GET  /api/now/table/{table}/{sys_id}``
- ``POST /api/now/table/{change_request|sysapproval_approver}``, ``PATCH|PUT /api/now/table/{table}/{sys_id}``

Tables: ``change_request``, ``sysapproval_approver``, ``sys_user``, ``sys_user_group``, ``cmdb_ci_service``.
All values are strings; reference fields come back as ``{"link", "value"}``, as in the real API.

Business rules, as the real instance would enforce them for an integration user:
- ``approval`` cannot be set to approved/rejected through the API; only approvers decide
  (``POST /_admin/servicenow/approvals/{sys_id}`` plays the approver).
- A change cannot move to Scheduled/Implement/Review/Closed (``-2``, ``-1``, ``0``, ``3``) until it is approved.
- There is no idempotency key; use ``correlation_id`` and query before you create (Phase 6).
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from release_copilot.mocks.auth import servicenow_auth
from release_copilot.mocks.state import get_world

router = APIRouter(prefix="/servicenow/api/now/table", tags=["servicenow"], dependencies=[Depends(servicenow_auth)])

REFERENCES = {
    "change_request": {"cmdb_ci": "cmdb_ci_service", "assignment_group": "sys_user_group", "requested_by": "sys_user"},
    "sysapproval_approver": {"sysapproval": "change_request", "approver": "sys_user", "group": "sys_user_group"},
    "sys_user_group": {"manager": "sys_user"},
    "cmdb_ci_service": {"support_group": "sys_user_group", "owned_by": "sys_user"},
    "sys_user": {},
}
WRITABLE = {"change_request", "sysapproval_approver"}
CHANGE_CHOICES = {
    "state": {"-5", "-4", "-3", "-2", "-1", "0", "3", "4"},  # New, Assess, Authorize, Scheduled, Implement, Review, Closed, Canceled
    "risk": {"1", "2", "3", "4"},  # Very High, High, Moderate, Low
    "impact": {"1", "2", "3"},
    "type": {"normal", "standard", "emergency"},
}
NEEDS_APPROVAL = {"-2", "-1", "0", "3"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _error(status: int, message: str, detail: str = "") -> HTTPException:
    return HTTPException(status, {"error": {"message": message, "detail": detail}, "status": "failure"})


def _table(world: dict[str, Any], table: str) -> list[dict[str, Any]]:
    tables = world["servicenow"]["tables"]
    if table not in tables:
        raise _error(400, "Invalid table " + table)
    return tables[table]


def find(world: dict[str, Any], table: str, sys_id: str) -> dict[str, Any] | None:
    return next((r for r in _table(world, table) if r["sys_id"] == sys_id), None)


def _display(world: dict[str, Any], table: str, sys_id: str) -> str:
    rec = find(world, table, sys_id) or {}
    return rec.get("number") or rec.get("name") or sys_id


def render(world: dict[str, Any], table: str, rec: dict[str, Any], request: Request, display: str = "false", fields: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    refs = REFERENCES.get(table, {})
    for key, value in rec.items():
        if fields and key not in fields:
            continue
        value = "" if value is None else str(value)
        if key in refs and value:
            link = f"{request.base_url}servicenow/api/now/table/{refs[key]}/{value}"
            if display == "true":
                out[key] = _display(world, refs[key], value)
            elif display == "all":
                out[key] = {"display_value": _display(world, refs[key], value), "link": link, "value": value}
            else:
                out[key] = {"link": link, "value": value}
        else:
            out[key] = value
    return out


def _matches(rec: dict[str, Any], cond: str) -> bool:
    for op in ("!=", "STARTSWITH", "LIKE", "IN", "="):
        if op in cond:
            field, value = cond.split(op, 1)
            actual = str(rec.get(field, "") or "")
            return {
                "!=": actual != value,
                "STARTSWITH": actual.startswith(value),
                "LIKE": value.lower() in actual.lower(),
                "IN": actual in value.split(","),
                "=": actual == value,
            }[op]
    raise _error(400, f"Unsupported query condition: {cond}")


def query(records: list[dict[str, Any]], sysparm_query: str) -> list[dict[str, Any]]:
    out, order = list(records), []
    for part in filter(None, sysparm_query.split("^")):
        if part.startswith("ORDERBYDESC"):
            order.append((part[11:], True))
        elif part.startswith("ORDERBY"):
            order.append((part[7:], False))
        elif part.startswith("OR") or part.startswith("NQ"):
            raise _error(400, "The mock Table API supports AND (^) conditions only.")
        else:
            out = [r for r in out if _matches(r, part)]
    for field, desc in reversed(order):
        out.sort(key=lambda r: str(r.get(field, "")), reverse=desc)
    return out


@router.get("/{table}")
def list_records(
    table: str,
    request: Request,
    response: Response,
    sysparm_query: str = "",
    sysparm_limit: int = 100,
    sysparm_offset: int = 0,
    sysparm_fields: str | None = None,
    sysparm_display_value: str = "false",
    world: dict = Depends(get_world),
) -> dict[str, Any]:
    rows = query(_table(world, table), sysparm_query)
    response.headers["X-Total-Count"] = str(len(rows))
    fields = sysparm_fields.split(",") if sysparm_fields else None
    page = rows[sysparm_offset : sysparm_offset + sysparm_limit]
    return {"result": [render(world, table, r, request, sysparm_display_value, fields) for r in page]}


@router.get("/{table}/{sys_id}")
def get_record(table: str, sys_id: str, request: Request, sysparm_display_value: str = "false", world: dict = Depends(get_world)) -> dict[str, Any]:
    rec = find(world, table, sys_id)
    if not rec:
        raise _error(404, "No Record found", "Record doesn't exist or ACL restricts the record retrieval")
    return {"result": render(world, table, rec, request, sysparm_display_value)}


def _validate_change(world: dict[str, Any], values: dict[str, Any], current: dict[str, Any] | None) -> None:
    for field, allowed in CHANGE_CHOICES.items():
        if field in values and str(values[field]) not in allowed:
            raise _error(400, f"Invalid value '{values[field]}' for field '{field}'", f"Allowed: {sorted(allowed)}")
    if str(values.get("approval", "")) in {"approved", "rejected"}:
        raise _error(403, "Operation Failed", "Approval can only be set by an approver, not by the integration user.")
    approval = (current or {}).get("approval", "not requested")
    if str(values.get("state", "")) in NEEDS_APPROVAL and approval != "approved":
        raise _error(403, "Operation Failed", f"Change cannot move to state {values['state']} before it is approved (approval={approval}).")
    for field, target in REFERENCES["change_request"].items():
        if values.get(field) and not find(world, target, str(values[field])):
            raise _error(400, f"Invalid reference for '{field}'", f"No {target} record with sys_id {values[field]}")


def _create_change(world: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    if not values.get("short_description"):
        raise _error(400, "Mandatory field missing", "short_description is mandatory")
    _validate_change(world, values, None)
    sn = world["servicenow"]
    number = f"CHG{sn['next_change_number']:07d}"
    sn["next_change_number"] += 1
    rec = {
        "type": "normal",
        "risk": "3",
        "impact": "3",
        "state": "-5",
        "approval": "not requested",
        **{k: str(v) for k, v in values.items()},
        "sys_id": uuid.uuid4().hex,
        "number": number,
        "sys_created_on": now(),
        "sys_updated_on": now(),
    }
    return rec


def _create_approver(world: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    change = find(world, "change_request", str(values.get("sysapproval", "")))
    if not change:
        raise _error(400, "Invalid reference for 'sysapproval'", "sysapproval must be a change_request sys_id")
    if not find(world, "sys_user", str(values.get("approver", ""))):
        raise _error(400, "Invalid reference for 'approver'", "approver must be a sys_user sys_id")
    if values.get("state") not in (None, "requested"):
        raise _error(403, "Operation Failed", "Approvers record their own decision.")
    change["approval"] = "requested"
    if change["state"] in {"-5", "-4"}:
        change["state"] = "-3"  # Authorize
    change["sys_updated_on"] = now()
    return {
        "sys_id": uuid.uuid4().hex,
        "sysapproval": change["sys_id"],
        "approver": str(values["approver"]),
        "group": str(values.get("group", "")),
        "state": "requested",
        "comments": str(values.get("comments", "")),
        "sys_created_on": now(),
        "sys_updated_on": now(),
    }


@router.post("/{table}", status_code=201)
def create_record(table: str, request: Request, payload: dict[str, Any] = Body(...), world: dict = Depends(get_world)) -> dict[str, Any]:
    rows = _table(world, table)
    if table not in WRITABLE:
        raise _error(403, "Operation Failed", f"ACL: insert on {table} is not allowed for this user")
    rec = _create_change(world, payload) if table == "change_request" else _create_approver(world, payload)
    rows.append(rec)
    return {"result": render(world, table, rec, request)}


@router.api_route("/{table}/{sys_id}", methods=["PATCH", "PUT"])
def update_record(table: str, sys_id: str, request: Request, payload: dict[str, Any] = Body(...), world: dict = Depends(get_world)) -> dict[str, Any]:
    if table not in WRITABLE:
        raise _error(403, "Operation Failed", f"ACL: write on {table} is not allowed for this user")
    rec = find(world, table, sys_id)
    if not rec:
        raise _error(404, "No Record found", "Record doesn't exist or ACL restricts the record retrieval")
    payload = {k: v for k, v in payload.items() if k not in {"sys_id", "number", "sys_created_on"}}
    if table == "change_request":
        _validate_change(world, payload, rec)
    elif "state" in payload:
        raise _error(403, "Operation Failed", "Approvers record their own decision.")
    rec.update({k: str(v) for k, v in payload.items()})
    rec["sys_updated_on"] = now()
    return {"result": render(world, table, rec, request)}


def decide_approval(world: dict[str, Any], sys_id: str, decision: str, comments: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Record a human approver's decision and roll it up onto the change (used by the admin router)."""
    approval = find(world, "sysapproval_approver", sys_id)
    if not approval:
        raise _error(404, "No Record found", f"No sysapproval_approver {sys_id}")
    approval.update({"state": decision, "comments": comments, "sys_updated_on": now()})
    change = find(world, "change_request", approval["sysapproval"])
    assert change is not None
    siblings = [a for a in _table(world, "sysapproval_approver") if a["sysapproval"] == change["sys_id"]]
    if any(a["state"] == "rejected" for a in siblings):
        change.update({"approval": "rejected", "state": "-5"})
    elif all(a["state"] == "approved" for a in siblings):
        change.update({"approval": "approved", "state": "-2"})
    change["sys_updated_on"] = now()
    return approval, change
