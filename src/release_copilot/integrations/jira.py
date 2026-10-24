"""Jira Cloud REST v3 client: release scope (issues in a fix version), comments, transitions.

YOUR PART. The contract is in tests/test_jira.py.
"""

from collections.abc import AsyncIterator, Sequence

from release_copilot.integrations.http import ServiceClient
from release_copilot.integrations.models import Issue, JiraComment

# Ask Jira only for what Issue needs: payloads stay small, and fields can't leak in by accident.
ISSUE_FIELDS = (
    "summary", "issuetype", "status", "priority", "assignee", "labels", "components", "fixVersions",
    "description", "comment", "created", "updated", "resolutiondate",
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

    def search(self, jql: str, *, page_size: int = 50, fields: Sequence[str] = ISSUE_FIELDS) -> AsyncIterator[Issue]:
        """All issues matching ``jql``, following ``nextPageToken`` across pages (``GET /rest/api/3/search/jql``)."""
        raise NotImplementedError

    async def get_issue(self, key: str) -> Issue:
        raise NotImplementedError

    async def add_comment(self, key: str, markdown: str) -> JiraComment:
        """Post a comment (Markdown is converted to ADF with ``markdown_to_adf``)."""
        raise NotImplementedError

    async def transition(self, key: str, to_status: str) -> None:
        """Move the issue to ``to_status`` (matched case-insensitively against the available transitions)."""
        raise NotImplementedError
