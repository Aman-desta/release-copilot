"""SonarQube Web API (subset). Base URL: ``/sonar``.

- ``GET /api/authentication/validate``
- ``GET /api/project_branches/list?project=``
- ``GET /api/measures/component?component=&metricKeys=&branch=`` (``new_*`` metrics come back under ``period``)
- ``GET /api/qualitygates/project_status?projectKey=&branch=``

Without ``branch`` the main branch is used; in this mock it holds the release-candidate code.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from release_copilot.mocks.auth import sonar_auth
from release_copilot.mocks.scenarios.base import RELEASE_BRANCH
from release_copilot.mocks.state import get_world

router = APIRouter(prefix="/sonar/api", tags=["sonar"], dependencies=[Depends(sonar_auth)])


def _branch(world: dict[str, Any], project: str, branch: str | None) -> tuple[dict[str, Any], dict[str, str]]:
    sonar = world["sonar"]
    if project != sonar["component"]["key"]:
        raise HTTPException(404, {"errors": [{"msg": f"Component key '{project}' not found"}]})
    name = branch or RELEASE_BRANCH
    if name not in sonar["branches"]:
        raise HTTPException(404, {"errors": [{"msg": f"Component '{project}' on branch '{name}' not found"}]})
    return sonar, sonar["branches"][name]


@router.get("/authentication/validate")
def validate() -> dict[str, bool]:
    return {"valid": True}


@router.get("/project_branches/list")
def branches(project: str, world: dict = Depends(get_world)) -> dict[str, Any]:
    sonar, _ = _branch(world, project, None)
    return {
        "branches": [
            {"name": "main", "isMain": True, "type": "BRANCH"},
            *({"name": n, "isMain": False, "type": "BRANCH"} for n in sonar["branches"]),
        ]
    }


@router.get("/measures/component")
def measures(
    component: str, metricKeys: str = Query(...), branch: str | None = None, world: dict = Depends(get_world)
) -> dict[str, Any]:
    sonar, values = _branch(world, component, None if branch == "main" else branch)
    out = []
    for key in (k.strip() for k in metricKeys.split(",")):
        if key not in values:
            continue
        if key.startswith("new_"):
            out.append({"metric": key, "period": {"index": 1, "value": values[key], "bestValue": False}})
        else:
            out.append({"metric": key, "value": values[key], "bestValue": False})
    return {"component": {**sonar["component"], "measures": out}}


@router.get("/qualitygates/project_status")
def project_status(projectKey: str, branch: str | None = None, world: dict = Depends(get_world)) -> dict[str, Any]:
    sonar, values = _branch(world, projectKey, None if branch == "main" else branch)
    conditions = []
    for cond in sonar["quality_gate"]["conditions"]:
        actual = float(values[cond["metricKey"]])
        threshold = float(cond["errorThreshold"])
        failed = actual < threshold if cond["comparator"] == "LT" else actual > threshold
        conditions.append({**cond, "status": "ERROR" if failed else "OK", "actualValue": values[cond["metricKey"]]})
    status = "ERROR" if any(c["status"] == "ERROR" for c in conditions) else "OK"
    return {"projectStatus": {"status": status, "conditions": conditions, "ignoredConditions": False}}
