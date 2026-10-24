"""GitHub REST client: the change set, PRs and reviews, checks, Dependabot and code-scanning alerts, releases."""

from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from release_copilot.integrations.errors import InvalidRequestError
from release_copilot.integrations.http import ServiceClient
from release_copilot.integrations.models import (
    ChangedFile,
    CheckRun,
    CodeScanningAlert,
    Commit,
    Comparison,
    DependencyAlert,
    PullRequest,
    Release,
    Review,
)

GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
REPO = "/repos/{owner}/{repo}"


class GitHubClient:
    def __init__(self, http: ServiceClient, *, owner: str, repo: str) -> None:
        self._http = http
        self._repo = {"owner": owner, "repo": repo}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------------------------- plumbing

    async def _get(self, path: str, *, params: Mapping[str, Any] | None = None, **path_params: Any) -> Any:
        return await self._http.get_json(REPO + path, path_params={**self._repo, **path_params}, params=params)

    async def _paginate(
        self, path: str, *, params: Mapping[str, Any] | None = None, page_size: int = 100, **path_params: Any
    ) -> AsyncIterator[dict[str, Any]]:
        """Follow ``Link: <…>; rel="next"`` until the last page.

        Only the next link's *query* is reused, on our own path template: that keeps logs low-cardinality and
        means a malicious or misconfigured link can never send our token to another host.
        """
        query: dict[str, Any] = {"per_page": page_size, **(params or {})}
        while True:
            response = await self._http.request("GET", REPO + path, path_params={**self._repo, **path_params}, params=query)
            for item in response.json():
                yield item
            next_link = response.links.get("next", {}).get("url")
            if not next_link:
                return
            query = dict(parse_qsl(urlsplit(next_link).query))

    # -------------------------------------------------------------------------------------- mapping

    @staticmethod
    def _commit(raw: dict[str, Any]) -> Commit:
        files = raw.get("files")
        return Commit(
            sha=raw["sha"],
            message=raw["commit"]["message"],
            author_name=raw["commit"]["author"]["name"],
            author_login=(raw.get("author") or {}).get("login"),
            date=raw["commit"]["author"]["date"],
            url=raw["html_url"],
            files=None if files is None else tuple(GitHubClient._file(f) for f in files),
        )

    @staticmethod
    def _file(raw: dict[str, Any]) -> ChangedFile:
        return ChangedFile(filename=raw["filename"], status=raw["status"], additions=raw["additions"], deletions=raw["deletions"])

    @staticmethod
    def _pull(raw: dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=raw["number"],
            title=raw["title"],
            state=raw["state"],
            merged=raw.get("merged_at") is not None,  # list endpoints omit "merged"; merged_at is always present
            merged_at=raw.get("merged_at"),
            author=raw["user"]["login"],
            base_ref=raw["base"]["ref"],
            head_ref=raw["head"]["ref"],
            merge_commit_sha=raw.get("merge_commit_sha"),
            url=raw["html_url"],
        )

    # ------------------------------------------------------------------------------------- read API

    async def compare(self, base: str, head: str) -> Comparison:
        raw = await self._get("/compare/{basehead}", basehead=f"{base}...{head}")
        # GitHub returns at most 250 commits per comparison; a bigger release needs the commits endpoint.
        return Comparison(
            base=base,
            head=head,
            status=raw["status"],
            ahead_by=raw["ahead_by"],
            total_commits=raw["total_commits"],
            commits=tuple(self._commit(c) for c in raw["commits"]),
            files=tuple(self._file(f) for f in raw.get("files", [])),
            url=raw["html_url"],
        )

    async def get_commit(self, ref: str) -> Commit:
        return self._commit(await self._get("/commits/{ref}", ref=ref))

    async def pulls_for_commit(self, sha: str) -> list[PullRequest]:
        return [self._pull(p) for p in await self._get("/commits/{sha}/pulls", sha=sha)]

    async def list_pulls(self, *, state: str = "closed", base: str | None = None, page_size: int = 100) -> AsyncIterator[PullRequest]:
        params = {"state": state, **({"base": base} if base else {})}
        async for raw in self._paginate("/pulls", params=params, page_size=page_size):
            yield self._pull(raw)

    async def reviews(self, number: int) -> list[Review]:
        return [
            Review(id=r["id"], reviewer=r["user"]["login"], state=r["state"], submitted_at=r.get("submitted_at"))
            for r in await self._get("/pulls/{number}/reviews", number=number)
        ]

    async def check_runs(self, ref: str) -> list[CheckRun]:
        raw = await self._get("/commits/{ref}/check-runs", ref=ref)
        return [
            CheckRun(name=r["name"], status=r["status"], conclusion=r.get("conclusion"), head_sha=r["head_sha"], completed_at=r.get("completed_at"))
            for r in raw["check_runs"]
        ]

    async def dependabot_alerts(self, *, state: str = "open", severity: str | None = None) -> list[DependencyAlert]:
        params = {"state": state, **({"severity": severity} if severity else {})}
        alerts = []
        for a in await self._get("/dependabot/alerts", params=params):
            advisory, vuln = a["security_advisory"], a["security_vulnerability"]
            alerts.append(
                DependencyAlert(
                    number=a["number"],
                    state=a["state"],
                    package=a["dependency"]["package"]["name"],
                    ecosystem=a["dependency"]["package"]["ecosystem"],
                    manifest_path=a["dependency"]["manifest_path"],
                    severity=advisory["severity"],
                    cve_id=advisory.get("cve_id"),
                    ghsa_id=advisory["ghsa_id"],
                    summary=advisory["summary"],
                    cvss_score=(advisory.get("cvss") or {}).get("score"),
                    vulnerable_range=vuln["vulnerable_version_range"],
                    patched_version=(vuln.get("first_patched_version") or {}).get("identifier"),
                    url=a["html_url"],
                )
            )
        return alerts

    async def code_scanning_alerts(self, *, state: str = "open", ref: str | None = None) -> list[CodeScanningAlert]:
        params = {"state": state, **({"ref": ref} if ref else {})}
        alerts = []
        for a in await self._get("/code-scanning/alerts", params=params):
            rule, instance = a["rule"], a["most_recent_instance"]
            alerts.append(
                CodeScanningAlert(
                    number=a["number"],
                    state=a["state"],
                    rule_id=rule["id"],
                    severity=rule.get("security_severity_level") or rule["severity"],
                    description=rule["description"],
                    tool=a["tool"]["name"],
                    path=instance["location"]["path"],
                    start_line=instance["location"]["start_line"],
                    ref=instance["ref"],
                    message=instance["message"]["text"],
                    url=a["html_url"],
                )
            )
        return alerts

    # ------------------------------------------------------------------------------------ write API

    @staticmethod
    def _release(raw: dict[str, Any]) -> Release:
        return Release(id=raw["id"], tag_name=raw["tag_name"], name=raw["name"], draft=raw["draft"], prerelease=raw["prerelease"], url=raw["html_url"])

    async def list_releases(self) -> list[Release]:
        return [self._release(r) async for r in self._paginate("/releases")]

    async def create_release(self, tag: str, *, name: str, body: str, target: str, draft: bool = True) -> Release:
        """Create a (draft) release, idempotently by tag: if the tag already has one, return it unchanged.

        POST is not retried by the HTTP layer, and a retry after a lost response would hit "already_exists";
        both cases end here with the one release for this tag.
        """
        payload = {"tag_name": tag, "name": name, "body": body, "target_commitish": target, "draft": draft}
        try:
            raw = await self._http.post_json(REPO + "/releases", path_params=self._repo, json=payload)
        except InvalidRequestError as exc:
            if "already_exists" not in exc.message and "already exists" not in exc.message.lower():
                raise
            existing = next((r for r in await self.list_releases() if r.tag_name == tag), None)
            if existing is None:
                raise
            return existing
        return self._release(raw)
