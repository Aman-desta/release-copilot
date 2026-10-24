"""Build integration clients from settings. The only place that knows which auth and headers each service uses.

``transport`` lets tests route a client into the in-process mocks (``httpx.ASGITransport``).
"""

import httpx

from release_copilot.config import Settings, get_settings
from release_copilot.integrations.auth import BearerToken, basic_auth
from release_copilot.integrations.github import GITHUB_HEADERS, GitHubClient
from release_copilot.integrations.http import ServiceClient
from release_copilot.integrations.jira import JiraClient


def jira_client(settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> JiraClient:
    s = (settings or get_settings()).jira
    base = str(s.base_url).rstrip("/")
    http = ServiceClient("jira", base, auth=basic_auth(s.email, s.api_token), transport=transport)
    return JiraClient(http, site_url=base)


def github_client(settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> GitHubClient:
    s = (settings or get_settings()).github
    http = ServiceClient("github", str(s.base_url), auth=BearerToken(s.token), headers=GITHUB_HEADERS, transport=transport)
    return GitHubClient(http, owner=s.owner, repo=s.repo)
