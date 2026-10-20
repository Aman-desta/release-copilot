"""Settings for the mock enterprise services (env prefix ``RELEASE_COPILOT_MOCK_``).

The default credentials are test values. Clients read the same values from ``.env``.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class MockSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RELEASE_COPILOT_MOCK_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 9100
    default_scenario: str = "clean"
    auth_enabled: bool = True

    # Jira Cloud: Basic auth with account email + API token.
    jira_email: str = "release-bot@acme-fin.test"
    jira_api_token: str = "jira-test-token"

    # GitHub: fine-grained PAT sent as "Bearer" or "token".
    github_token: str = "ghp_testtoken000000000000000000000000"

    # Entra ID app registration used for Azure ARM (client-credentials flow).
    azure_tenant_id: str = "11111111-1111-4111-8111-111111111111"
    azure_client_id: str = "22222222-2222-4222-8222-222222222222"
    azure_client_secret: str = "azure-test-secret"
    azure_token_ttl_seconds: int = 3600

    # SonarQube user token.
    sonar_token: str = "squ_testtoken"

    # ServiceNow: Basic auth integration user.
    servicenow_user: str = "release.copilot"
    servicenow_password: str = "snow-test-password"

    # How long a "timeout" fault sleeps before answering.
    timeout_fault_seconds: float = 30.0


@lru_cache
def get_settings() -> MockSettings:
    return MockSettings()
