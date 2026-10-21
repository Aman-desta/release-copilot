"""Model factory: one place that turns ``settings.llm`` into chat and embedding models (story F3).

YOUR PART. The contract is in tests/test_llm.py.
"""

from collections.abc import Callable
from functools import lru_cache
from typing import Any, Literal

import openai
from langchain_core.embeddings import Embeddings
from langchain_core.embeddings.fake import DeterministicFakeEmbedding
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings, ChatOpenAI, OpenAIEmbeddings
from pydantic import SecretStr

from release_copilot.config import LLMSettings, RoleModel, get_settings
from release_copilot.llm.fake import FakeChatModel

Role = Literal["supervisor", "worker", "writer", "judge"]
ROLES: tuple[Role, ...] = ("supervisor", "worker", "writer", "judge")

TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


def _model_settings(role: Role) -> tuple[LLMSettings, RoleModel]:
    if role not in ROLES:
        raise ValueError(f"unknown model role: {role}")
    settings = get_settings().llm
    return settings, getattr(settings, role)


def _required_secret(value: SecretStr | None, field: str, provider: str) -> SecretStr:
    if value is None or not value.get_secret_value():
        raise ValueError(f"{provider} provider requires {field}")
    return value


def _chat_model(role: Role, llm: LLMSettings, model: RoleModel, model_name: str | None = None) -> BaseChatModel:
    name = model_name or model.model
    kwargs: dict[str, Any] = {
        "model": name,
        "timeout": llm.timeout_seconds,
        "max_retries": llm.max_retries,
        "metadata": {"copilot_role": role},
    }
    if model.temperature is not None:
        kwargs["temperature"] = model.temperature

    if llm.provider == "fake":
        return FakeChatModel(model_name=name, metadata={"copilot_role": role})
    if llm.provider == "openai":
        return ChatOpenAI(api_key=_required_secret(llm.openai_api_key, "openai_api_key", llm.provider), **kwargs)
    if llm.provider == "azure_openai":
        if llm.azure_endpoint is None:
            raise ValueError("azure_openai provider requires azure_endpoint")
        azure_kwargs: dict[str, Any] = {
            "azure_deployment": name,
            "azure_endpoint": str(llm.azure_endpoint),
            "api_key": _required_secret(llm.azure_api_key, "azure_api_key", llm.provider),
            "api_version": llm.azure_api_version,
            "timeout": llm.timeout_seconds,
            "max_retries": llm.max_retries,
            "metadata": {"copilot_role": role},
        }
        if model.temperature is not None:
            azure_kwargs["temperature"] = model.temperature
        return AzureChatOpenAI(**azure_kwargs)
    if llm.provider == "litellm":
        if llm.litellm_base_url is None:
            raise ValueError("litellm provider requires litellm_base_url")
        return ChatOpenAI(
            base_url=str(llm.litellm_base_url),
            api_key=_required_secret(llm.litellm_api_key, "litellm_api_key", llm.provider),
            **kwargs,
        )
    raise ValueError(f"unsupported model provider: {llm.provider}")


@lru_cache
def get_chat_model(role: Role) -> BaseChatModel:
    """The primary model for ``role``. Cached per role; must not call the network."""
    llm, model = _model_settings(role)
    return _chat_model(role, llm, model)


def get_resilient_model(role: Role, prepare: Callable[[BaseChatModel], Runnable] | None = None) -> Runnable:
    """Primary model with the role's fallback (if configured), with ``prepare`` applied to each model first."""
    llm, role_model = _model_settings(role)
    primary = get_chat_model(role)
    prepare_model = prepare or (lambda model: model)
    prepared_primary = prepare_model(primary)
    if not role_model.fallback:
        return prepared_primary

    fallback = _chat_model(role, llm, role_model, role_model.fallback)
    prepared_fallback = prepare_model(fallback)
    return prepared_primary.with_fallbacks(
        [prepared_fallback],
        exceptions_to_handle=TRANSIENT_ERRORS,
    )


@lru_cache
def get_embeddings() -> Embeddings:
    llm = get_settings().llm
    if llm.provider == "fake":
        return DeterministicFakeEmbedding(size=llm.embedding_dimensions)
    if llm.provider == "azure_openai":
        if llm.azure_endpoint is None:
            raise ValueError("azure_openai provider requires azure_endpoint")
        return AzureOpenAIEmbeddings(
            azure_deployment=llm.embedding_model,
            azure_endpoint=str(llm.azure_endpoint),
            api_key=_required_secret(llm.azure_api_key, "azure_api_key", llm.provider),
            api_version=llm.azure_api_version,
            dimensions=llm.embedding_dimensions,
            timeout=llm.timeout_seconds,
            max_retries=llm.max_retries,
        )

    if llm.provider == "litellm":
        if llm.litellm_base_url is None:
            raise ValueError("litellm provider requires litellm_base_url")
        base_url = str(llm.litellm_base_url)
        api_key = _required_secret(llm.litellm_api_key, "litellm_api_key", llm.provider)
    else:
        base_url = None
        api_key = _required_secret(llm.openai_api_key, "openai_api_key", llm.provider)
    return OpenAIEmbeddings(
        model=llm.embedding_model,
        dimensions=llm.embedding_dimensions,
        api_key=api_key,
        base_url=base_url,
        timeout=llm.timeout_seconds,
        max_retries=llm.max_retries,
    )
