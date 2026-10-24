"""Shared fixtures: an isolated settings environment, and the mock enterprise services in-process (no network)."""

from collections.abc import AsyncIterator

import httpx
import pytest
from release_copilot_mocks.app import create_app

from release_copilot.config import get_settings

# A complete, valid environment for Settings. Secret values are listed in SECRETS so tests can assert they never leak.
BASE_ENV = {
    "RELEASE_COPILOT_ENV": "local",
    "RELEASE_COPILOT_DATABASE_URL": "postgresql+psycopg://copilot_app:app-pw@localhost:5432/release_copilot",
    "RELEASE_COPILOT_JIRA__BASE_URL": "http://localhost:9100/jira",
    "RELEASE_COPILOT_JIRA__EMAIL": "release-bot@acme-fin.test",
    "RELEASE_COPILOT_JIRA__API_TOKEN": "jira-tok",
    "RELEASE_COPILOT_JIRA__PROJECT_KEY": "PAY",
    "RELEASE_COPILOT_GITHUB__BASE_URL": "http://localhost:9100/github",
    "RELEASE_COPILOT_GITHUB__TOKEN": "gh-tok",
    "RELEASE_COPILOT_GITHUB__OWNER": "acme-fin",
    "RELEASE_COPILOT_GITHUB__REPO": "payments-api",
    "RELEASE_COPILOT_AZURE__AUTHORITY_URL": "http://localhost:9100/azure/login",
    "RELEASE_COPILOT_AZURE__MANAGEMENT_URL": "http://localhost:9100/azure/management",
    "RELEASE_COPILOT_AZURE__TENANT_ID": "11111111-1111-4111-8111-111111111111",
    "RELEASE_COPILOT_AZURE__CLIENT_ID": "22222222-2222-4222-8222-222222222222",
    "RELEASE_COPILOT_AZURE__CLIENT_SECRET": "az-secret",
    "RELEASE_COPILOT_AZURE__SUBSCRIPTION_ID": "6f1c2a9e-3b4d-4e5f-8a7b-0c1d2e3f4a5b",
    "RELEASE_COPILOT_AZURE__RESOURCE_GROUP": "rg-payments-prod",
    "RELEASE_COPILOT_AZURE__SITE": "app-payments-api-prod",
    "RELEASE_COPILOT_SONAR__BASE_URL": "http://localhost:9100/sonar",
    "RELEASE_COPILOT_SONAR__TOKEN": "sonar-tok",
    "RELEASE_COPILOT_SONAR__PROJECT_KEY": "payments-api",
    "RELEASE_COPILOT_SERVICENOW__BASE_URL": "http://localhost:9100/servicenow",
    "RELEASE_COPILOT_SERVICENOW__USER": "release.copilot",
    "RELEASE_COPILOT_SERVICENOW__PASSWORD": "snow-pw",
    "RELEASE_COPILOT_NOTIFY__TEAMS_WEBHOOK_URL": "http://localhost:9100/notify/teams/teams-hook-secret",
    "RELEASE_COPILOT_LLM__PROVIDER": "openai",
    "RELEASE_COPILOT_LLM__OPENAI_API_KEY": "llm-key",
}
SECRETS = ["app-pw", "jira-tok", "gh-tok", "az-secret", "sonar-tok", "snow-pw", "teams-hook-secret", "llm-key"]


def _clear_caches() -> None:
    get_settings.cache_clear()
    try:
        from release_copilot.llm import factory
    except ImportError:  # the factory is written in story F3
        return
    for fn in (factory.get_chat_model, factory.get_embeddings):
        if hasattr(fn, "cache_clear"):
            fn.cache_clear()


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    """Isolated settings: BASE_ENV only. Runs in a temp dir so a developer's .env is never read."""
    monkeypatch.chdir(tmp_path)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    _clear_caches()
    yield monkeypatch
    _clear_caches()


@pytest.fixture
def mock_env(app_env):
    """``app_env`` with the mocks' own test credentials, for clients built from settings (``integrations.clients``)."""
    from release_copilot_mocks.settings import MockSettings

    m = MockSettings()
    for key, value in {
        "JIRA__EMAIL": m.jira_email, "JIRA__API_TOKEN": m.jira_api_token, "GITHUB__TOKEN": m.github_token,
        "AZURE__TENANT_ID": m.azure_tenant_id, "AZURE__CLIENT_ID": m.azure_client_id,
        "AZURE__CLIENT_SECRET": m.azure_client_secret, "SONAR__TOKEN": m.sonar_token,
        "SERVICENOW__USER": m.servicenow_user, "SERVICENOW__PASSWORD": m.servicenow_password,
    }.items():
        app_env.setenv(f"RELEASE_COPILOT_{key}", value)
    get_settings.cache_clear()
    return app_env


@pytest.fixture
def mocks_app():
    """A fresh mock app per test (default scenario ``clean``; override per request with ``X-Mock-Scenario``)."""
    return create_app("clean")


@pytest.fixture
async def mocks_http(mocks_app) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client routed into the in-process mocks. Clients under test can reuse its transport."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mocks_app), base_url="http://mocks") as client:
        yield client
