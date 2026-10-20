import base64

import pytest
from fastapi.testclient import TestClient

from release_copilot.mocks.app import create_app
from release_copilot.mocks.scenarios.base import RESOURCE_GROUP, SITE, SUBSCRIPTION_ID
from release_copilot.mocks.settings import get_settings

S = get_settings()
SITE_URL = f"/azure/management/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Web/sites/{SITE}"
API_VERSION = {"api-version": "2023-12-01"}


def basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


JIRA = basic(S.jira_email, S.jira_api_token)
GITHUB = {"Authorization": f"Bearer {S.github_token}"}
SONAR = {"Authorization": f"Bearer {S.sonar_token}"}
SNOW = basic(S.servicenow_user, S.servicenow_password)


def scenario(name: str) -> dict[str, str]:
    return {"X-Mock-Scenario": name}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app("clean"))


@pytest.fixture
def azure(client: TestClient) -> dict[str, str]:
    resp = client.post(
        f"/azure/login/{S.azure_tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": S.azure_client_id,
            "client_secret": S.azure_client_secret,
            "scope": "https://management.azure.com/.default",
        },
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}
