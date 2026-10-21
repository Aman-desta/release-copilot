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

class RoleModel(_Section):
    model: str
    fallback: str | None = None
    temperature: float | None = None  # None = provider default (for reasoning models, usually 0)
    timeout_seconds: float | None = None  # None = LLMSettings.timeout_seconds


class LLMSettings(_Section):
    provider: Literal["openai", "azure_openai", "litellm", "fake"] = "openai"

    openai_api_key: SecretStr | None = None

    azure_endpoint: AnyHttpUrl | None = None
    azure_api_key: SecretStr | None = None
    azure_api_version: str = "2024-10-21"

    litellm_base_url: AnyHttpUrl | None = None
    litellm_api_key: SecretStr | None = None

    # Per-request timeout and SDK retries (exponential backoff on connection errors, 408/409/429/5xx).
    # Worst-case latency of one call is about (retries + 1) x timeout + backoff, so keep these tight.
    timeout_seconds: float = 30.0
    max_retries: int = 3
    # When a role has a fallback, its primary retries only this often before the fallback takes over;
    # otherwise an outage waits through every primary retry first. The fallback keeps max_retries.
    retries_before_fallback: int = Field(default=1, ge=0)

    supervisor: RoleModel = Field(default_factory=lambda: RoleModel(model="gpt-4.1-mini", temperature=0.0))
    worker: RoleModel = Field(default_factory=lambda: RoleModel(model="gpt-4.1-mini", temperature=0.0))
    writer: RoleModel = Field(
        # Long release documents can legitimately take longer than a tool-calling turn.
        default_factory=lambda: RoleModel(model="gpt-4.1", fallback="gpt-4.1-mini", temperature=0.0, timeout_seconds=120.0)
    )
    # The judge must be at least as capable as the writer it grades; a reasoning model, so no temperature.
    # Prefer a different model family than the writer once a second provider is configured.
    judge: RoleModel = Field(default_factory=lambda: RoleModel(model="o4-mini", timeout_seconds=120.0))
    allow_judge_same_as_writer: bool = False

    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1536

    @model_validator(mode="after")
    def _provider_configuration(self) -> "LLMSettings":
        missing: list[str] = []
        if self.provider == "openai":
            if self.openai_api_key is None or not self.openai_api_key.get_secret_value():
                missing.append("openai_api_key")
        elif self.provider == "azure_openai":
            if self.azure_endpoint is None:
                missing.append("azure_endpoint")
            if self.azure_api_key is None or not self.azure_api_key.get_secret_value():
                missing.append("azure_api_key")
        elif self.provider == "litellm":
            if self.litellm_base_url is None:
                missing.append("litellm_base_url")
            if self.litellm_api_key is None or not self.litellm_api_key.get_secret_value():
                missing.append("litellm_api_key")

        if missing:
            raise ValueError(f"{self.provider} provider requires: {', '.join(missing)}")
        if not self.allow_judge_same_as_writer and self.judge.model == self.writer.model:
            raise ValueError("judge model must differ from writer model unless allow_judge_same_as_writer is true")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RELEASE_COPILOT_",
        env_nested_delimiter="__",
        env_file=".env",
        env_ignore_empty=True,
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
    llm: LLMSettings = Field(default_factory=LLMSettings)

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
