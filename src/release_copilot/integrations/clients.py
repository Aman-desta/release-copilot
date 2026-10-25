"""Build integration clients from settings. The only place that knows which auth and headers each service uses.

``transport`` lets tests route a client into the in-process mocks (``httpx.ASGITransport``).
"""

import httpx

from release_copilot.config import Settings, get_settings
from release_copilot.integrations.auth import BearerToken, OAuth2ClientCredentials, basic_auth
from release_copilot.integrations.azure import AzureAppServiceClient
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


ARM_SCOPE = "https://management.azure.com/.default"  # the ARM scope, whatever host the management URL uses


def azure_client(settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> AzureAppServiceClient:
    s = (settings or get_settings()).azure
    token_http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False)
    auth = OAuth2ClientCredentials(
        token_url=f"{str(s.authority_url).rstrip('/')}/{s.tenant_id}/oauth2/v2.0/token",
        client_id=s.client_id,
        client_secret=s.client_secret,
        scope=ARM_SCOPE,
        http=token_http,
    )
    http = ServiceClient("azure", str(s.management_url), auth=auth, transport=transport)
    client = AzureAppServiceClient(http, subscription_id=s.subscription_id, resource_group=s.resource_group, site=s.site)
    client.on_close(token_http.aclose)
    return client
