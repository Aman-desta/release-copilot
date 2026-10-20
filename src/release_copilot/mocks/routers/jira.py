"""Jira Cloud REST API v3 (subset). Base URL: ``/jira``.

- ``GET  /rest/api/3/myself``
- ``GET  /rest/api/3/search/jql`` (current endpoint; ``nextPageToken`` paging) and the legacy ``GET /rest/api/3/search`` (``startAt`` paging)
- ``GET  /rest/api/3/issue/{key}``, ``GET|POST /rest/api/3/issue/{key}/comment``
- ``GET|POST /rest/api/3/issue/{key}/transitions``
- ``GET  /rest/api/3/project/{key}``, ``GET /rest/api/3/project/{key}/versions``

JQL support is a small subset: ``AND``-joined clauses on project, fixVersion, status, issuetype/type, key,
labels, component, priority, assignee with ``=``, ``!=``, ``in (…)``, ``not in (…)``; ``text ~ "…"``; and
``ORDER BY key|created|updated [ASC|DESC]``. Anything else returns 400, like Jira does for bad JQL.
"""

import base64
import copy
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from release_copilot.mocks import adf
from release_copilot.mocks.auth import jira_auth
from release_copilot.mocks.state import get_world

router = APIRouter(prefix="/jira/rest/api/3", tags=["jira"], dependencies=[Depends(jira_auth)])

FIELD_GETTERS = {
    "project": lambda f: [f["project"]["key"]],
    "fixversion": lambda f: [v["name"] for v in f["fixVersions"]],
    "status": lambda f: [f["status"]["name"]],
    "issuetype": lambda f: [f["issuetype"]["name"]],
    "type": lambda f: [f["issuetype"]["name"]],
    "labels": lambda f: f["labels"],
    "component": lambda f: [c["name"] for c in f["components"]],
    "priority": lambda f: [f["priority"]["name"]],
    "assignee": lambda f: [f["assignee"]["accountId"], f["assignee"]["displayName"]] if f["assignee"] else [],
}
TRANSITIONS = [
    {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
    {"id": "21", "name": "In Progress", "to": {"name": "In Progress"}},
    {"id": "31", "name": "Done", "to": {"name": "Done"}},
    {"id": "41", "name": "Released", "to": {"name": "Released"}},
]
CLAUSE = re.compile(
    r'^\s*(?P<field>\w+)\s*(?P<op>!=|=|~|not\s+in|in)\s*(?P<value>\(.*\)|"[^"]*"|\'[^\']*\'|[^\s]+)\s*$',
    re.IGNORECASE,
)


def _jql_error(msg: str) -> HTTPException:
    return HTTPException(400, {"errorMessages": [msg], "errors": {}})


def _values(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("("):
        return [v.strip().strip("\"'") for v in raw[1:-1].split(",") if v.strip()]
    return [raw.strip("\"'")]


def parse_jql(jql: str) -> tuple[list[tuple[str, str, list[str]]], tuple[str, bool] | None]:
    jql = jql.strip()
    order: tuple[str, bool] | None = None
    m = re.search(r"\border\s+by\s+(\w+)(?:\s+(asc|desc))?\s*$", jql, re.IGNORECASE)
    if m:
        order = (m.group(1).lower(), (m.group(2) or "asc").lower() == "desc")
        jql = jql[: m.start()].strip()
    if re.search(r"\bor\b", jql, re.IGNORECASE):
        raise _jql_error("The mock JQL parser supports AND only.")
    clauses = []
    for part in re.split(r"\s+and\s+", jql, flags=re.IGNORECASE) if jql else []:
        cm = CLAUSE.match(part)
        if not cm:
            raise _jql_error(f"Error in the JQL Query: unable to parse '{part}'.")
        field, op = cm.group("field").lower(), re.sub(r"\s+", " ", cm.group("op").lower())
        if field not in FIELD_GETTERS and field not in {"key", "text", "summary"}:
            raise _jql_error(f"Field '{cm.group('field')}' does not exist or you do not have permission to view it.")
        clauses.append((field, op, _values(cm.group("value"))))
    return clauses, order


def _match(issue: dict[str, Any], field: str, op: str, values: list[str]) -> bool:
    f = issue["fields"]
    if op == "~":
        needle = values[0].lower()
        haystack = f["summary"] if field == "summary" else f"{f['summary']}\n{adf.to_text(f['description'])}"
        return needle in haystack.lower()
    actual = [issue["key"]] if field == "key" else FIELD_GETTERS[field](f)
    hit = any(a.lower() == v.lower() for a in actual for v in values)
    return not hit if op in {"!=", "not in"} else hit


def _render(issue: dict[str, Any], request: Request, fields: list[str] | None) -> dict[str, Any]:
    out = copy.deepcopy(issue)
    out["self"] = str(request.base_url) + f"jira/rest/api/3/issue/{issue['id']}"
    if fields and "*all" not in fields:
        out["fields"] = {k: v for k, v in out["fields"].items() if k in fields}
    return out


def _search(world: dict[str, Any], jql: str) -> list[dict[str, Any]]:
    clauses, order = parse_jql(jql)
    issues = [i for i in world["jira"]["issues"].values() if all(_match(i, *c) for c in clauses)]
    key, desc = order or ("key", False)
    sort_key = {
        "key": lambda i: int(i["key"].split("-")[1]),
        "created": lambda i: i["fields"]["created"],
        "updated": lambda i: i["fields"]["updated"],
    }.get(key)
    if sort_key is None:
        raise _jql_error(f"Not able to sort using field '{key}'.")
    return sorted(issues, key=sort_key, reverse=desc)


def _fields_param(fields: str | None) -> list[str] | None:
    return [f.strip() for f in fields.split(",")] if fields else None


def _issue(world: dict[str, Any], key: str) -> dict[str, Any]:
    issue = world["jira"]["issues"].get(key.upper())
    if not issue:
        raise HTTPException(404, {"errorMessages": ["Issue does not exist or you do not have permission to see it."], "errors": {}})
    return issue


@router.get("/myself")
def myself(world: dict = Depends(get_world)) -> dict[str, Any]:
    return {"accountId": "712020:release-bot", "displayName": "Release Copilot", "emailAddress": "release-bot@acme-fin.test", "active": True}


@router.get("/search/jql")
def search_jql(
    request: Request,
    jql: str = Query(...),
    fields: str | None = None,
    maxResults: int = Query(50, ge=1, le=100),
    nextPageToken: str | None = None,
    world: dict = Depends(get_world),
) -> dict[str, Any]:
    issues = _search(world, jql)
    start = int(base64.urlsafe_b64decode(nextPageToken).decode()) if nextPageToken else 0
    page = issues[start : start + maxResults]
    body: dict[str, Any] = {"issues": [_render(i, request, _fields_param(fields)) for i in page], "isLast": start + maxResults >= len(issues)}
    if not body["isLast"]:
        body["nextPageToken"] = base64.urlsafe_b64encode(str(start + maxResults).encode()).decode()
    return body


@router.get("/search")
def search_legacy(
    request: Request,
    jql: str = Query(""),
    fields: str | None = None,
    startAt: int = Query(0, ge=0),
    maxResults: int = Query(50, ge=1, le=100),
    world: dict = Depends(get_world),
) -> dict[str, Any]:
    issues = _search(world, jql)
    return {
        "startAt": startAt,
        "maxResults": maxResults,
        "total": len(issues),
        "issues": [_render(i, request, _fields_param(fields)) for i in issues[startAt : startAt + maxResults]],
    }


@router.get("/issue/{key}")
def get_issue(key: str, request: Request, fields: str | None = None, world: dict = Depends(get_world)) -> dict[str, Any]:
    return _render(_issue(world, key), request, _fields_param(fields))


@router.get("/issue/{key}/comment")
def get_comments(key: str, world: dict = Depends(get_world)) -> dict[str, Any]:
    comments = _issue(world, key)["fields"]["comment"]["comments"]
    return {"startAt": 0, "maxResults": len(comments), "total": len(comments), "comments": comments}


@router.post("/issue/{key}/comment", status_code=201)
def add_comment(key: str, payload: dict[str, Any] = Body(...), world: dict = Depends(get_world)) -> dict[str, Any]:
    body = payload.get("body")
    if not isinstance(body, dict) or body.get("type") != "doc":
        raise HTTPException(400, {"errorMessages": [], "errors": {"comment": "Comment body must be an Atlassian Document Format (ADF) document."}})
    comments = _issue(world, key)["fields"]["comment"]["comments"]
    comment = {
        "id": str(30000 + sum(len(i["fields"]["comment"]["comments"]) for i in world["jira"]["issues"].values())),
        "author": {"accountId": "712020:release-bot", "displayName": "Release Copilot"},
        "body": body,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
    }
    comments.append(comment)
    return comment


@router.get("/issue/{key}/transitions")
def get_transitions(key: str, world: dict = Depends(get_world)) -> dict[str, Any]:
    _issue(world, key)
    return {"transitions": TRANSITIONS}


@router.post("/issue/{key}/transitions", status_code=204)
def do_transition(key: str, payload: dict[str, Any] = Body(...), world: dict = Depends(get_world)) -> None:
    issue = _issue(world, key)
    tid = str(payload.get("transition", {}).get("id", ""))
    target = next((t for t in TRANSITIONS if t["id"] == tid), None)
    if not target:
        raise HTTPException(400, {"errorMessages": [f"Transition id '{tid}' is not valid for this issue."], "errors": {}})
    name = target["to"]["name"]
    issue["fields"]["status"] = {"name": name, "statusCategory": {"key": "done" if name in {"Done", "Released"} else "indeterminate"}}


@router.get("/project/{key}")
def get_project(key: str, world: dict = Depends(get_world)) -> dict[str, Any]:
    project = world["jira"]["project"]
    if key.upper() != project["key"]:
        raise HTTPException(404, {"errorMessages": [f"No project could be found with key '{key}'."], "errors": {}})
    return project


@router.get("/project/{key}/versions")
def get_versions(key: str, world: dict = Depends(get_world)) -> list[dict[str, Any]]:
    get_project(key, world)
    return world["jira"]["versions"]
