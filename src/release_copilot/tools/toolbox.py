"""The Toolbox: owns the integration clients and hands each agent exactly the tools it may use.

    async with Toolbox.from_settings() as box:
        evidence_tools = box.tools(domains={"jira", "github"})          # read-only by default
        writer_tools = box.tools(domains={"jira"}, access={"read", "write"})

Read-only is the default: a caller has to ask for write tools explicitly, so evidence workers can't get them
by accident. High-risk writes (change requests, approvals, slot swaps) are not tools at all: they're
deterministic workflow steps behind human approval (A6–A8).
"""

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

import httpx
from langchain_core.tools import BaseTool

from release_copilot.config import Settings, get_settings
from release_copilot.integrations import clients
from release_copilot.integrations.azure import AzureAppServiceClient
from release_copilot.integrations.github import GitHubClient
from release_copilot.integrations.jira import JiraClient
from release_copilot.integrations.servicenow import ServiceNowClient
from release_copilot.integrations.sonar import SonarClient
from release_copilot.tools.base import Access

DOMAINS = ("jira", "github", "sonar", "azure", "servicenow")


@dataclass
class Toolbox:
    settings: Settings
    jira: JiraClient
    github: GitHubClient
    sonar: SonarClient
    azure: AzureAppServiceClient
    servicenow: ServiceNowClient
    _built: list[BaseTool] | None = field(default=None, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> "Toolbox":
        s = settings or get_settings()
        return cls(
            settings=s,
            jira=clients.jira_client(s, transport=transport),
            github=clients.github_client(s, transport=transport),
            sonar=clients.sonar_client(s, transport=transport),
            azure=clients.azure_client(s, transport=transport),
            servicenow=clients.servicenow_client(s, transport=transport),
        )

    async def aclose(self) -> None:
        for client in (self.jira, self.github, self.sonar, self.azure, self.servicenow):
            await client.aclose()

    async def __aenter__(self) -> "Toolbox":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def all_tools(self) -> list[BaseTool]:
        if self._built is None:
            from release_copilot.tools import azure, github, jira, servicenow, sonar

            builders: list[Callable[[Toolbox], list[BaseTool]]] = [jira.build, github.build, sonar.build, azure.build, servicenow.build]
            self._built = [tool for build in builders for tool in build(self)]
            names = [t.name for t in self._built]
            duplicates = {n for n in names if names.count(n) > 1}
            if duplicates:
                raise ValueError(f"duplicate tool names: {sorted(duplicates)}")
        return self._built

    def tools(self, *, domains: Collection[str] | None = None, access: Collection[Access] = ("read",)) -> list[BaseTool]:
        """Tools filtered by domain and access. Read-only unless ``access`` explicitly includes ``"write"``."""
        unknown = set(domains or ()) - set(DOMAINS)
        if unknown:
            raise ValueError(f"unknown tool domains: {sorted(unknown)}")
        return [
            t for t in self.all_tools()
            if _meta(t, "access") in access and (domains is None or _meta(t, "domain") in domains)
        ]


def _meta(tool: BaseTool, key: str) -> Any:
    return (tool.metadata or {}).get(key)
