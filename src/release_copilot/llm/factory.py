"""Model factory: one place that turns ``settings.llm`` into chat and embedding models (story F3).

- ``get_chat_model(role)``: the role's primary model, cached; carries ``metadata["copilot_role"]`` for usage/traces.
- ``get_resilient_model(role, prepare)``: primary plus the role's fallback; ``prepare`` (bind_tools,
  with_structured_output, ...) is applied to each model *before* ``with_fallbacks``, whose result has neither.
- ``get_embeddings()``: embeddings for memory and policy RAG.

Credentials and endpoints are validated by ``LLMSettings`` at startup, so the factory does not re-check them.
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

# Worth retrying on another model. Not 400/401/403/404: those are bugs or config errors a fallback would hide.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,  # includes APITimeoutError
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


def _model_settings(role: Role) -> tuple[LLMSettings, RoleModel]:
    if role not in ROLES:
        raise ValueError(f"unknown model role: {role}")
    llm = get_settings().llm
    return llm, getattr(llm, role)


def _secret(value: SecretStr | None) -> SecretStr:
    assert value is not None, "validated by LLMSettings"
    return value


def _chat_model(role: Role, llm: LLMSettings, role_model: RoleModel, *, name: str, max_retries: int) -> BaseChatModel:
    metadata = {"copilot_role": role}
    if llm.provider == "fake":
        return FakeChatModel(model_name=name, metadata=metadata)

    common: dict[str, Any] = {
        "timeout": role_model.timeout_seconds or llm.timeout_seconds,
        "max_retries": max_retries,
        "metadata": metadata,
    }
    if role_model.temperature is not None:
        common["temperature"] = role_model.temperature

    if llm.provider == "azure_openai":
        # On Azure the configured "model" is the deployment name.
        return AzureChatOpenAI(
            azure_deployment=name,
            azure_endpoint=str(llm.azure_endpoint),
            api_key=_secret(llm.azure_api_key),
            api_version=llm.azure_api_version,
            **common,
        )
    if llm.provider == "litellm":
        return ChatOpenAI(model=name, base_url=str(llm.litellm_base_url), api_key=_secret(llm.litellm_api_key), **common)
    return ChatOpenAI(model=name, api_key=_secret(llm.openai_api_key), **common)


@lru_cache
def get_chat_model(role: Role) -> BaseChatModel:
    """The primary model for ``role``, with the full retry budget. Cached per role; never calls the network."""
    llm, role_model = _model_settings(role)
    return _chat_model(role, llm, role_model, name=role_model.model, max_retries=llm.max_retries)


def get_resilient_model(role: Role, prepare: Callable[[BaseChatModel], Runnable] | None = None) -> Runnable:
    """Primary with the role's fallback (if configured), with ``prepare`` applied to each model first.

    With a fallback, the primary retries only ``retries_before_fallback`` times, so an outage switches models in
    seconds instead of waiting through every primary retry; the fallback keeps the full ``max_retries``.
    """
    llm, role_model = _model_settings(role)
    prepare = prepare or (lambda model: model)
    if not role_model.fallback:
        return prepare(get_chat_model(role))

    primary = _chat_model(role, llm, role_model, name=role_model.model, max_retries=llm.retries_before_fallback)
    fallback = _chat_model(role, llm, role_model, name=role_model.fallback, max_retries=llm.max_retries)
    return prepare(primary).with_fallbacks([prepare(fallback)], exceptions_to_handle=TRANSIENT_ERRORS)


@lru_cache
def get_embeddings() -> Embeddings:
    llm = get_settings().llm
    if llm.provider == "fake":
        return DeterministicFakeEmbedding(size=llm.embedding_dimensions)
    common: dict[str, Any] = {
        "dimensions": llm.embedding_dimensions,
        "timeout": llm.timeout_seconds,
        "max_retries": llm.max_retries,
    }
    if llm.provider == "azure_openai":
        return AzureOpenAIEmbeddings(
            azure_deployment=llm.embedding_model,
            azure_endpoint=str(llm.azure_endpoint),
            api_key=_secret(llm.azure_api_key),
            api_version=llm.azure_api_version,
            **common,
        )
    if llm.provider == "litellm":
        return OpenAIEmbeddings(model=llm.embedding_model, base_url=str(llm.litellm_base_url), api_key=_secret(llm.litellm_api_key), **common)
    return OpenAIEmbeddings(model=llm.embedding_model, api_key=_secret(llm.openai_api_key), **common)
