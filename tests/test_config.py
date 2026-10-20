"""Spec for ``release_copilot.config`` (story F2). These tests describe the contract; make them pass."""

import pytest
from pydantic import ValidationError

from release_copilot.config import Settings, SettingsError, get_settings, load_settings

SECRETS = ["app-pw", "jira-tok", "gh-tok", "az-secret", "sonar-tok", "snow-pw", "teams-hook-secret"]


@pytest.fixture
def env(monkeypatch):
    values = {
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
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def load() -> Settings:
    return Settings(_env_file=None)  # tests must not depend on a developer's .env


def test_nested_settings_load_from_env(env):
    s = load()
    assert s.env == "local"
    assert s.db_schema == "copilot"  # default
    assert str(s.jira.base_url).rstrip("/") == "http://localhost:9100/jira"
    assert s.jira.api_token.get_secret_value() == "jira-tok"
    assert s.azure.client_secret.get_secret_value() == "az-secret"
    assert s.github.owner == "acme-fin" and s.sonar.project_key == "payments-api"
    assert s.notify.slack_webhook_url is None  # webhooks are optional


def test_secrets_never_appear_in_repr_or_dumps(env):
    s = load()
    rendered = " ".join([repr(s), str(s), s.model_dump_json(), str(s.model_dump())])
    for secret in SECRETS:
        assert secret not in rendered, secret


def test_missing_required_secret_fails_fast(env):
    env.delenv("RELEASE_COPILOT_GITHUB__TOKEN")
    with pytest.raises(ValidationError, match="token"):
        load()


def test_database_url_must_use_psycopg3_driver(env):
    env.setenv("RELEASE_COPILOT_DATABASE_URL", "postgresql://copilot_app:app-pw@localhost:5432/release_copilot")
    with pytest.raises(ValidationError, match="postgresql\\+psycopg"):
        load()


def test_libpq_conninfo_for_langgraph(env):
    # SQLAlchemy/Alembic want "postgresql+psycopg://"; psycopg and the LangGraph checkpointer want plain libpq.
    assert load().database_conninfo() == "postgresql://copilot_app:app-pw@localhost:5432/release_copilot"


def test_prod_refuses_superuser_connections(env):
    env.setenv("RELEASE_COPILOT_ENV", "prod")
    env.setenv("RELEASE_COPILOT_DATABASE_URL", "postgresql+psycopg://postgres:pw@db:5432/release_copilot")
    with pytest.raises(ValidationError, match="superuser"):
        load()


def test_get_settings_is_cached(env):
    assert get_settings() is get_settings()


def test_load_errors_never_contain_secrets(env):
    env.setenv("RELEASE_COPILOT_DATABASE_URL", "postgresql://copilot_app:app-pw@localhost:5432/release_copilot")
    env.delenv("RELEASE_COPILOT_JIRA__EMAIL")
    with pytest.raises(SettingsError) as info:
        load_settings(_env_file=None)
    err = info.value
    assert "database_url" in str(err) and "jira.email" in str(err)  # every problem is reported at once
    assert err.__cause__ is None and err.__suppress_context__  # the raw ValidationError is not chained
    for secret in SECRETS:
        assert secret not in str(err)


def test_schema_name_must_be_a_plain_identifier(env):
    env.setenv("RELEASE_COPILOT_DB_SCHEMA", 'x"; DROP TABLE t; --')
    with pytest.raises(ValidationError, match="db_schema"):
        load()


def test_integration_urls_are_validated_at_startup(env):
    env.setenv("RELEASE_COPILOT_JIRA__BASE_URL", "htp//oops")
    with pytest.raises(ValidationError, match="jira.base_url"):
        load()


@pytest.fixture
def prod(env):
    env.setenv("RELEASE_COPILOT_ENV", "prod")
    env.setenv("RELEASE_COPILOT_DATABASE_URL", "postgresql+psycopg://copilot_app:app-pw@db:5432/release_copilot?sslmode=verify-full")
    for key in ("JIRA__BASE_URL", "GITHUB__BASE_URL", "AZURE__AUTHORITY_URL", "AZURE__MANAGEMENT_URL", "SONAR__BASE_URL", "SERVICENOW__BASE_URL"):
        env.setenv(f"RELEASE_COPILOT_{key}", "https://example.test/api")
    env.setenv("RELEASE_COPILOT_NOTIFY__TEAMS_WEBHOOK_URL", "https://example.test/hook/teams-hook-secret")
    return env


def test_prod_accepts_a_hardened_config(prod):
    assert load().env == "prod"


def test_prod_requires_tls_to_the_database(prod):
    prod.setenv("RELEASE_COPILOT_DATABASE_URL", "postgresql+psycopg://copilot_app:app-pw@db:5432/release_copilot")
    with pytest.raises(ValidationError, match="sslmode"):
        load()


def test_prod_requires_https_integrations(prod):
    prod.setenv("RELEASE_COPILOT_JIRA__BASE_URL", "http://jira.internal")
    prod.setenv("RELEASE_COPILOT_NOTIFY__TEAMS_WEBHOOK_URL", "http://hooks.internal/teams-hook-secret")
    with pytest.raises(ValidationError, match="jira.base_url, notify.teams_webhook_url") as info:
        load()
    assert "teams-hook-secret" not in str(info.value)
