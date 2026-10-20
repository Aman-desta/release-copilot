"""Application settings, loaded once from the environment (and ``.env`` in the working directory).

Variables use the prefix ``RELEASE_COPILOT_``; nested models use ``__``, e.g. ``RELEASE_COPILOT_JIRA__API_TOKEN``.
Run commands from the repo root so ``.env`` is found; in containers, inject real environment variables instead.

Always load through ``get_settings()``: it fails fast at startup with an error that never contains input values.
"""

import re
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DB_URL_PREFIX = "postgresql+psycopg://"
SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")  # plain, unquoted Postgres identifier
PROD_SSLMODES = {"require", "verify-ca", "verify-full"}


class _Section(BaseModel):
    # str(ValidationError) must never echo raw values (they may be secrets). See also load_settings().
    model_config = ConfigDict(hide_input_in_errors=True, frozen=True)


class JiraSettings(_Section):
    base_url: AnyHttpUrl
    email: str
    api_token: SecretStr
    project_key: str


class GitHubSettings(_Section):
    base_url: AnyHttpUrl
    token: SecretStr
    owner: str
    repo: str


class AzureSettings(_Section):
    authority_url: AnyHttpUrl
    management_url: AnyHttpUrl
    tenant_id: str
    client_id: str
    client_secret: SecretStr
    subscription_id: str
    resource_group: str
    site: str


class SonarSettings(_Section):
    base_url: AnyHttpUrl
    token: SecretStr
    project_key: str


class ServiceNowSettings(_Section):
    base_url: AnyHttpUrl
    user: str
    password: SecretStr


class NotifySettings(_Section):
    # Incoming-webhook URLs carry their own credential, so they are secrets.
    teams_webhook_url: SecretStr | None = None
    slack_webhook_url: SecretStr | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RELEASE_COPILOT_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    env: Literal["local", "test", "prod"] = "local"
    database_url: SecretStr
    db_schema: str = "copilot"
    jira: JiraSettings
    github: GitHubSettings
    azure: AzureSettings
    sonar: SonarSettings
    servicenow: ServiceNowSettings
    notify: NotifySettings = Field(default_factory=NotifySettings)

    @field_validator("database_url")
    @classmethod
    def _psycopg3_driver(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(DB_URL_PREFIX):
            raise ValueError(f"database_url must start with {DB_URL_PREFIX}")
        return value

    @field_validator("db_schema")
    @classmethod
    def _plain_identifier(cls, value: str) -> str:
        # The schema name is interpolated into SQL (search_path, Alembic, LangGraph setup): allow nothing that needs quoting.
        if not SCHEMA_NAME.fullmatch(value):
            raise ValueError("db_schema must match ^[a-z_][a-z0-9_]{0,62}$")
        return value

    @model_validator(mode="after")
    def _production_rules(self) -> "Settings":
        if self.env != "prod":
            return self
        url = urlsplit(self.database_url.get_secret_value())
        if url.username is not None and unquote(url.username) == "postgres":
            raise ValueError("production database connections must not use the postgres superuser")
        if parse_qs(url.query).get("sslmode", [""])[0] not in PROD_SSLMODES:
            raise ValueError(f"production database_url needs sslmode in {sorted(PROD_SSLMODES)}")
        insecure = [name for name, value in self._integration_urls() if not value.startswith("https://")]
        if insecure:
            raise ValueError(f"production integration URLs must use https: {', '.join(insecure)}")
        return self

    def _integration_urls(self) -> list[tuple[str, str]]:
        urls: list[tuple[str, str]] = []
        for section_name in ("jira", "github", "azure", "sonar", "servicenow", "notify"):
            section = getattr(self, section_name)
            for field_name in type(section).model_fields:
                value = getattr(section, field_name)
                if isinstance(value, AnyHttpUrl):
                    urls.append((f"{section_name}.{field_name}", str(value)))
                elif isinstance(value, SecretStr) and field_name.endswith("_url"):
                    urls.append((f"{section_name}.{field_name}", value.get_secret_value()))
        return urls

    def database_conninfo(self) -> str:
        """Plain libpq URL (``postgresql://``) for psycopg and the LangGraph checkpointer/store."""
        return self.database_url.get_secret_value().replace(DB_URL_PREFIX, "postgresql://", 1)


class SettingsError(RuntimeError):
    """Invalid configuration. The message lists field locations and reasons only, never input values."""


def load_settings(**overrides: object) -> Settings:
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'settings'}: {e['msg']}" for e in exc.errors())
        # "from None" drops the original exception, whose .errors() still holds the raw (secret) inputs.
        raise SettingsError(f"Invalid configuration ({exc.error_count()} errors): {problems}") from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()
