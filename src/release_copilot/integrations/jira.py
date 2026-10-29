"""Jira Cloud REST v3 client: release scope (issues in a fix version), comments, transitions.

Completeness rule: this client feeds release approvals, so it must never *silently* return partial evidence.
Pagination is driven by ``nextPageToken`` (a missing ``isLast`` can't end it early, a stuck token raises), and
comments beyond the page embedded in an issue are fetched from the comment endpoint.
"""

from collections.abc import AsyncIterator, Sequence
from typing import Any

from release_copilot.integrations.adf import adf_to_markdown, markdown_to_adf
from release_copilot.integrations.errors import IntegrationError, InvalidRequestError
from release_copilot.integrations.http import ServiceClient
from release_copilot.integrations.models import Issue, JiraComment

ISSUE_PATH = "/rest/api/3/issue/{key}"
COMMENT_PAGE_SIZE = 100
# Ask Jira only for what Issue needs: payloads stay small, and fields can't leak in by accident.
ISSUE_FIELDS = (
    "summary", "issuetype", "status", "priority", "assignee", "labels", "components",
    "description", "comment", "created", "updated"
)


class JiraClient:
    def __init__(self, http: ServiceClient, *, site_url: str) -> None:
        """``site_url`` is the browser base for issue links: ``{site_url}/browse/{key}``."""
        self._http = http
        self._site_url = site_url.rstrip("/")

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "JiraClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def search(self, jql: str, *, page_size: int = 50, fields: Sequence[str] = ISSUE_FIELDS) -> AsyncIterator[Issue]:
        """All issues matching ``jql``, following ``nextPageToken`` across pages (``GET /rest/api/3/search/jql``)."""
        params: dict[str, Any] = {
            "jql": jql,
            "fields": ",".join(fields),
            "maxResults": page_size,
        }
        seen_tokens: set[str] = set()
        while True:
            page = await self._http.get_json("/rest/api/3/search/jql", params=params)
            for raw in page.get("issues", []):
                if isinstance(raw, dict):
                    yield await self._complete(raw)
            # The token drives paging; isLast is only an early exit (a missing isLast must not truncate results).
            next_page_token = page.get("nextPageToken")
            if page.get("isLast") is True or not isinstance(next_page_token, str) or not next_page_token:
                return
            if next_page_token in seen_tokens:
                raise IntegrationError("search pagination did not advance (repeated nextPageToken)", service="jira", method="GET", path="/rest/api/3/search/jql")
            seen_tokens.add(next_page_token)
            params["nextPageToken"] = next_page_token

    async def get_issue(self, key: str) -> Issue:
        raw = await self._http.get_json(ISSUE_PATH, path_params={"key": key}, params={"fields": ",".join(ISSUE_FIELDS)})
        return await self._complete(raw)

    async def _complete(self, raw: dict[str, Any]) -> Issue:
        """Map an issue, fetching the remaining comments when the embedded comment page is truncated."""
        issue = self._issue(raw)
        embedded = (raw.get("fields") or {}).get("comment") or {}
        total = embedded.get("total") if isinstance(embedded, dict) else None
        if isinstance(total, int) and total > len(issue.comments):
            issue = issue.model_copy(update={"comments": tuple(await self._all_comments(issue.key, total))})
        return issue

    async def _all_comments(self, key: str, total: int) -> list[JiraComment]:
        comments: list[JiraComment] = []
        while len(comments) < total:
            page = await self._http.get_json(
                ISSUE_PATH + "/comment",
                path_params={"key": key},
                params={"startAt": len(comments), "maxResults": COMMENT_PAGE_SIZE, "orderBy": "created"},
            )
            batch = [self._comment(c) for c in page.get("comments", []) if isinstance(c, dict)]
            if not batch:
                break  # the issue lost comments between calls; return what exists
            comments.extend(batch)
            total = page.get("total", total) if isinstance(page.get("total"), int) else total
        return comments

    async def add_comment(self, key: str, markdown: str) -> JiraComment:
        """Post a comment (Markdown is converted to ADF with ``markdown_to_adf``)."""
        raw = await self._http.post_json(
            ISSUE_PATH + "/comment",
            path_params={"key": key},
            json={"body": markdown_to_adf(markdown)},
        )
        return self._comment(raw)

    async def transition(self, key: str, to_status: str) -> None:
        """Move the issue to ``to_status`` (matched case-insensitively against the available transitions)."""
        path = ISSUE_PATH + "/transitions"
        path_params = {"key": key}
        response = await self._http.get_json(path, path_params=path_params)
        transitions = response.get("transitions", []) if isinstance(response, dict) else []
        target = None
        available: list[str] = []
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            destination = transition.get("to")
            name = destination.get("name") if isinstance(destination, dict) else None
            if not isinstance(name, str):
                continue
            available.append(name)
            if name.casefold() == to_status.casefold():
                target = transition
        if target is None:
            raise InvalidRequestError(
                f"Cannot transition to {to_status!r}; available statuses: {', '.join(available) or 'none'}",
                service="jira",
                path=path,
            )

        await self._http.request(
            "POST",
            path,
            path_params=path_params,
            json={"transition": {"id": target.get("id")}},
        )

    def _issue(self, raw: dict[str, Any]) -> Issue:
        fields = raw["fields"]
        status = fields["status"]
        assignee = fields.get("assignee")
        priority = fields.get("priority")
        comment_data = fields.get("comment") or {}
        comments = comment_data.get("comments", []) if isinstance(comment_data, dict) else []
        return Issue(
            key=raw["key"],
            summary=fields["summary"],
            issue_type=fields["issuetype"]["name"],
            status=status["name"],
            status_category=status["statusCategory"]["key"],
            priority=priority.get("name") if isinstance(priority, dict) else None,
            assignee=assignee.get("displayName") if isinstance(assignee, dict) else None,
            labels=tuple(fields.get("labels") or ()),
            components=tuple(
                component["name"]
                for component in fields.get("components", [])
                if isinstance(component, dict) and isinstance(component.get("name"), str)
            ),
            fix_versions=tuple(
                version["name"]
                for version in fields.get("fixVersions", [])
                if isinstance(version, dict) and isinstance(version.get("name"), str)
            ),
            description_markdown=adf_to_markdown(fields.get("description")),
            comments=tuple(self._comment(comment) for comment in comments if isinstance(comment, dict)),
            created=fields["created"],
            updated=fields["updated"],
            resolved=fields.get("resolutiondate"),
            url=f"{self._site_url}/browse/{raw['key']}",
        )

    @staticmethod
    def _comment(raw: dict[str, Any]) -> JiraComment:
        author = raw.get("author")
        return JiraComment(
            id=str(raw["id"]),
            author=author.get("displayName", "") if isinstance(author, dict) else "",
            body_markdown=adf_to_markdown(raw.get("body")),
            created=raw["created"],
        )
