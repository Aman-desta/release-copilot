"""GitHub REST API (subset). Base URL: ``/github`` (works as PyGithub/githubkit ``base_url``).

Repos, compare, commits (+ PRs for a commit, check runs), pulls (+ reviews), Dependabot alerts,
code-scanning alerts, tags and releases. List endpoints page with ``per_page``/``page`` and a ``Link`` header,
as GitHub does.
"""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response

from release_copilot.mocks.auth import github_auth
from release_copilot.mocks.state import get_world

router = APIRouter(prefix="/github", tags=["github"], dependencies=[Depends(github_auth)])

SEVERITY_ORDER = ["low", "medium", "high", "critical"]


def _not_found() -> HTTPException:
    return HTTPException(404, {"message": "Not Found", "documentation_url": "https://docs.github.com/rest"})


def _repo(world: dict[str, Any], owner: str, repo: str) -> dict[str, Any]:
    gh = world["github"]
    if f"{owner}/{repo}".lower() != gh["repo"]["full_name"]:
        raise _not_found()
    return gh


def _resolve(gh: dict[str, Any], ref: str) -> int:
    """Index of ``ref`` (branch, tag or sha prefix) in the linear commit history."""
    sha = gh["refs"].get(ref.removeprefix("refs/heads/").removeprefix("refs/tags/"), ref)
    for i, c in enumerate(gh["commits"]):
        if len(sha) >= 7 and c["sha"].startswith(sha):
            return i
    raise HTTPException(404, {"message": f"No commit found for SHA: {ref}", "documentation_url": "https://docs.github.com/rest"})


def _commit_view(c: dict[str, Any], request: Request, owner: str, repo: str, with_files: bool) -> dict[str, Any]:
    out = {
        "sha": c["sha"],
        "commit": c["commit"],
        "author": c["author"],
        "html_url": f"https://github.com/{owner}/{repo}/commit/{c['sha']}",
        "url": f"{request.base_url}github/repos/{owner}/{repo}/commits/{c['sha']}",
    }
    if with_files:
        out["files"] = c["files"]
        out["stats"] = {
            "additions": sum(f["additions"] for f in c["files"]),
            "deletions": sum(f["deletions"] for f in c["files"]),
            "total": sum(f["changes"] for f in c["files"]),
        }
    return out


def _paginate(items: list[Any], request: Request, response: Response, per_page: int, page: int) -> list[Any]:
    last = max(1, -(-len(items) // per_page))
    links = []
    for rel, p in (("next", page + 1), ("last", last), ("prev", page - 1), ("first", 1)):
        if (rel in {"next", "last"} and page < last) or (rel in {"prev", "first"} and page > 1):
            links.append(f'<{request.url.include_query_params(page=p, per_page=per_page)}>; rel="{rel}"')
    if links:
        response.headers["Link"] = ", ".join(links)
    return items[(page - 1) * per_page : page * per_page]


def _pull_view(p: dict[str, Any], owner: str, repo: str) -> dict[str, Any]:
    out = {k: v for k, v in p.items() if not k.startswith("_")}
    out["html_url"] = f"https://github.com/{owner}/{repo}/pull/{p['number']}"
    out["merged"] = p["merged_at"] is not None
    return out


@router.get("/repos/{owner}/{repo}")
def get_repo(owner: str, repo: str, world: dict = Depends(get_world)) -> dict[str, Any]:
    return _repo(world, owner, repo)["repo"]


@router.get("/repos/{owner}/{repo}/compare/{basehead:path}")
def compare(owner: str, repo: str, basehead: str, request: Request, world: dict = Depends(get_world)) -> dict[str, Any]:
    gh = _repo(world, owner, repo)
    if "..." not in basehead:
        raise HTTPException(404, {"message": "Not Found"})
    base, head = basehead.split("...", 1)
    bi, hi = _resolve(gh, base), _resolve(gh, head)
    commits = gh["commits"][bi + 1 : hi + 1] if hi >= bi else []
    files: dict[str, dict[str, Any]] = {}
    for c in commits:
        for f in c["files"]:
            agg = files.setdefault(f["filename"], {**f, "additions": 0, "deletions": 0, "changes": 0})
            if f["status"] == "added":
                agg["status"] = "added"
            for k in ("additions", "deletions", "changes"):
                agg[k] += f[k]
    return {
        "status": "ahead" if hi > bi else "identical" if hi == bi else "behind",
        "ahead_by": max(0, hi - bi),
        "behind_by": max(0, bi - hi),
        "total_commits": len(commits),
        "base_commit": _commit_view(gh["commits"][bi], request, owner, repo, False),
        "merge_base_commit": _commit_view(gh["commits"][min(bi, hi)], request, owner, repo, False),
        "commits": [_commit_view(c, request, owner, repo, False) for c in commits],
        "files": list(files.values()),
        "html_url": f"https://github.com/{owner}/{repo}/compare/{basehead}",
    }


@router.get("/repos/{owner}/{repo}/commits")
def list_commits(
    owner: str,
    repo: str,
    request: Request,
    response: Response,
    sha: str = "main",
    per_page: int = Query(30, ge=1, le=100),
    page: int = Query(1, ge=1),
    world: dict = Depends(get_world),
) -> list[dict[str, Any]]:
    gh = _repo(world, owner, repo)
    history = list(reversed(gh["commits"][: _resolve(gh, sha) + 1]))  # newest first
    return [_commit_view(c, request, owner, repo, False) for c in _paginate(history, request, response, per_page, page)]


@router.get("/repos/{owner}/{repo}/commits/{ref}")
def get_commit(owner: str, repo: str, ref: str, request: Request, world: dict = Depends(get_world)) -> dict[str, Any]:
    gh = _repo(world, owner, repo)
    return _commit_view(gh["commits"][_resolve(gh, ref)], request, owner, repo, True)


@router.get("/repos/{owner}/{repo}/commits/{ref}/pulls")
def pulls_for_commit(owner: str, repo: str, ref: str, world: dict = Depends(get_world)) -> list[dict[str, Any]]:
    gh = _repo(world, owner, repo)
    sha = gh["commits"][_resolve(gh, ref)]["sha"]
    return [_pull_view(p, owner, repo) for p in gh["pulls"] if p["merge_commit_sha"] == sha]


@router.get("/repos/{owner}/{repo}/commits/{ref}/check-runs")
def check_runs(owner: str, repo: str, ref: str, world: dict = Depends(get_world)) -> dict[str, Any]:
    gh = _repo(world, owner, repo)
    runs = gh["check_runs"].get(gh["commits"][_resolve(gh, ref)]["sha"], [])
    return {"total_count": len(runs), "check_runs": runs}


@router.get("/repos/{owner}/{repo}/pulls")
def list_pulls(
    owner: str,
    repo: str,
    request: Request,
    response: Response,
    state: str = "open",
    base: str | None = None,
    per_page: int = Query(30, ge=1, le=100),
    page: int = Query(1, ge=1),
    world: dict = Depends(get_world),
) -> list[dict[str, Any]]:
    gh = _repo(world, owner, repo)
    pulls = [p for p in reversed(gh["pulls"]) if state in {"all", p["state"]} and (base is None or p["base"]["ref"] == base)]
    return [_pull_view(p, owner, repo) for p in _paginate(pulls, request, response, per_page, page)]


@router.get("/repos/{owner}/{repo}/pulls/{number}")
def get_pull(owner: str, repo: str, number: int, world: dict = Depends(get_world)) -> dict[str, Any]:
    gh = _repo(world, owner, repo)
    for p in gh["pulls"]:
        if p["number"] == number:
            return _pull_view(p, owner, repo)
    raise _not_found()


@router.get("/repos/{owner}/{repo}/pulls/{number}/reviews")
def get_reviews(owner: str, repo: str, number: int, world: dict = Depends(get_world)) -> list[dict[str, Any]]:
    gh = _repo(world, owner, repo)
    for p in gh["pulls"]:
        if p["number"] == number:
            return p["_reviews"]
    raise _not_found()


@router.get("/repos/{owner}/{repo}/dependabot/alerts")
def dependabot_alerts(
    owner: str, repo: str, state: str | None = None, severity: str | None = None, world: dict = Depends(get_world)
) -> list[dict[str, Any]]:
    alerts = _repo(world, owner, repo)["dependabot_alerts"]
    wanted = set(severity.split(",")) if severity else None
    return [
        {**a, "html_url": f"https://github.com/{owner}/{repo}/security/dependabot/{a['number']}"}
        for a in alerts
        if (state is None or a["state"] in state.split(",")) and (wanted is None or a["security_advisory"]["severity"] in wanted)
    ]


@router.get("/repos/{owner}/{repo}/code-scanning/alerts")
def code_scanning_alerts(
    owner: str, repo: str, state: str | None = None, ref: str | None = None, world: dict = Depends(get_world)
) -> list[dict[str, Any]]:
    alerts = _repo(world, owner, repo)["code_scanning_alerts"]
    return [
        {**a, "html_url": f"https://github.com/{owner}/{repo}/security/code-scanning/{a['number']}"}
        for a in alerts
        if (state is None or a["state"] == state)
        and (ref is None or a["most_recent_instance"]["ref"] in {ref, f"refs/heads/{ref}"})
    ]


@router.get("/repos/{owner}/{repo}/tags")
def list_tags(owner: str, repo: str, world: dict = Depends(get_world)) -> list[dict[str, Any]]:
    gh = _repo(world, owner, repo)
    return [{"name": name, "commit": {"sha": sha}} for name, sha in gh["refs"].items() if name.startswith("v")]


@router.get("/repos/{owner}/{repo}/releases")
def list_releases(owner: str, repo: str, world: dict = Depends(get_world)) -> list[dict[str, Any]]:
    return _repo(world, owner, repo)["releases"]


@router.post("/repos/{owner}/{repo}/releases", status_code=201)
def create_release(owner: str, repo: str, payload: dict[str, Any] = Body(...), world: dict = Depends(get_world)) -> dict[str, Any]:
    gh = _repo(world, owner, repo)
    tag = payload.get("tag_name")
    if not tag:
        raise HTTPException(422, {"message": "Validation Failed", "errors": [{"resource": "Release", "code": "missing_field", "field": "tag_name"}]})
    if any(r["tag_name"] == tag for r in gh["releases"]):
        raise HTTPException(422, {"message": "Validation Failed", "errors": [{"resource": "Release", "code": "already_exists", "field": "tag_name"}]})
    release = {
        "id": 990000 + len(gh["releases"]) + 1,
        "tag_name": tag,
        "target_commitish": payload.get("target_commitish", "main"),
        "name": payload.get("name", tag),
        "body": payload.get("body", ""),
        "draft": bool(payload.get("draft", False)),
        "prerelease": bool(payload.get("prerelease", False)),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "html_url": f"https://github.com/{owner}/{repo}/releases/tag/{tag}",
    }
    gh["releases"].append(release)
    return release
