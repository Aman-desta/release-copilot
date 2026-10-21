"""Spec for the model gateway (story F3): ``LLMSettings`` in config.py and ``release_copilot/llm/factory.py``.

No test makes a network call: building a model never contacts the provider; only invoking it does, and these
tests invoke only fake models.
"""

import openai
import pytest
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings, ChatOpenAI, OpenAIEmbeddings
from pydantic import BaseModel, ValidationError

from release_copilot.config import Settings
from release_copilot.llm import ROLES, UsageTracker, get_chat_model, get_embeddings, get_resilient_model
from release_copilot.llm.factory import TRANSIENT_ERRORS
from release_copilot.llm.fake import FakeChatModel
from release_copilot.llm.pricing import price_for


def load() -> Settings:
    return Settings(_env_file=None)


# ------------------------------------------------------------------------------------------- settings


def test_role_defaults(app_env):
    llm = load().llm
    assert ROLES == ("supervisor", "worker", "writer", "judge")
    assert llm.provider == "openai"
    assert llm.judge.model != llm.writer.model
    assert llm.judge.temperature in (None, 0)  # judges are deterministic (None = provider default, for reasoning models)
    assert llm.embedding_dimensions == 1536


def test_role_override_from_env(app_env):
    app_env.setenv("RELEASE_COPILOT_LLM__WORKER__MODEL", "gpt-4.1-nano")
    app_env.setenv("RELEASE_COPILOT_LLM__WORKER__FALLBACK", "gpt-4o-mini")
    llm = load().llm
    assert (llm.worker.model, llm.worker.fallback) == ("gpt-4.1-nano", "gpt-4o-mini")
    assert llm.supervisor.model  # other roles keep their defaults


def test_judge_must_differ_from_writer_unless_overridden(app_env):
    app_env.setenv("RELEASE_COPILOT_LLM__JUDGE__MODEL", load().llm.writer.model)
    with pytest.raises(ValidationError, match="judge"):
        load()
    app_env.setenv("RELEASE_COPILOT_LLM__ALLOW_JUDGE_SAME_AS_WRITER", "true")
    assert load().llm.judge.model == load().llm.writer.model


@pytest.mark.parametrize(
    "provider,missing",
    [("openai", ["RELEASE_COPILOT_LLM__OPENAI_API_KEY"]), ("azure_openai", []), ("litellm", [])],
)
def test_provider_credentials_are_required(app_env, provider, missing):
    app_env.setenv("RELEASE_COPILOT_LLM__PROVIDER", provider)
    for key in missing:
        app_env.delenv(key)
    with pytest.raises(ValidationError, match=provider):
        load()


def test_llm_secrets_are_masked(app_env):
    assert "llm-key" not in load().model_dump_json()


# -------------------------------------------------------------------------------------------- factory


def test_openai_models_per_role(app_env):
    model = get_chat_model("writer")
    s = load().llm
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == s.writer.model
    assert model.request_timeout == s.writer.timeout_seconds and model.max_retries == s.max_retries
    assert model.metadata["copilot_role"] == "writer"  # UsageTracker and traces attribute calls by this
    assert get_chat_model("writer") is get_chat_model("writer")  # cached per role


def test_temperature_none_is_not_sent(app_env):
    app_env.setenv("RELEASE_COPILOT_LLM__JUDGE__MODEL", "o4-mini")
    app_env.delenv("RELEASE_COPILOT_LLM__JUDGE__TEMPERATURE", raising=False)
    assert get_chat_model("judge").temperature is None  # reasoning models reject a temperature


def test_azure_openai_uses_deployments(app_env):
    app_env.setenv("RELEASE_COPILOT_LLM__PROVIDER", "azure_openai")
    app_env.setenv("RELEASE_COPILOT_LLM__AZURE_ENDPOINT", "https://acme-fin.openai.azure.com")
    app_env.setenv("RELEASE_COPILOT_LLM__AZURE_API_KEY", "llm-key")
    app_env.setenv("RELEASE_COPILOT_LLM__WORKER__MODEL", "worker-deployment")
    model = get_chat_model("worker")
    assert isinstance(model, AzureChatOpenAI)
    assert model.deployment_name == "worker-deployment"  # on Azure, the configured "model" is the deployment name
    assert model.azure_endpoint.rstrip("/") == "https://acme-fin.openai.azure.com"
    assert model.openai_api_version == load().llm.azure_api_version
    assert isinstance(get_embeddings(), AzureOpenAIEmbeddings)


def test_litellm_proxy_is_openai_compatible(app_env):
    app_env.setenv("RELEASE_COPILOT_LLM__PROVIDER", "litellm")
    app_env.setenv("RELEASE_COPILOT_LLM__LITELLM_BASE_URL", "http://localhost:4000")
    app_env.setenv("RELEASE_COPILOT_LLM__LITELLM_API_KEY", "llm-key")
    model = get_chat_model("supervisor")
    assert isinstance(model, ChatOpenAI)
    assert model.openai_api_base.rstrip("/") == "http://localhost:4000"


def test_embeddings(app_env):
    emb = get_embeddings()
    assert isinstance(emb, OpenAIEmbeddings) and emb.dimensions == 1536
    app_env.setenv("RELEASE_COPILOT_LLM__PROVIDER", "fake")
    from tests.conftest import _clear_caches

    _clear_caches()
    fake = get_embeddings()
    assert isinstance(fake, Embeddings) and len(fake.embed_query("x")) == 1536
    assert fake.embed_query("same") == fake.embed_query("same")  # deterministic


def test_unknown_role_is_rejected(app_env):
    with pytest.raises(ValueError, match="role"):
        get_chat_model("planner")


# ------------------------------------------------------------------------------------ fake + fallback


@pytest.fixture
def fake(app_env):
    app_env.setenv("RELEASE_COPILOT_LLM__PROVIDER", "fake")
    return app_env


def test_fake_provider_returns_fake_models(fake):
    model = get_chat_model("worker")
    assert isinstance(model, FakeChatModel) and model.metadata["copilot_role"] == "worker"


async def test_fallback_on_transient_error(fake):
    fake.setenv("RELEASE_COPILOT_LLM__WRITER__MODEL", "fail-primary")
    fake.setenv("RELEASE_COPILOT_LLM__WRITER__FALLBACK", "fake-backup")
    reply = await get_resilient_model("writer").ainvoke("write the release notes")
    assert reply.response_metadata["model_name"] == "fake-backup"


def test_fallback_only_for_transient_errors():
    # A 400 (bad prompt, bad schema) must surface: falling back would hide a bug and double the cost.
    assert openai.RateLimitError in TRANSIENT_ERRORS and openai.APIConnectionError in TRANSIENT_ERRORS
    assert openai.InternalServerError in TRANSIENT_ERRORS
    assert not any(issubclass(openai.BadRequestError, e) for e in TRANSIENT_ERRORS)
    assert not any(issubclass(openai.AuthenticationError, e) for e in TRANSIENT_ERRORS)


class Verdict(BaseModel):
    decision: str


async def test_prepare_is_applied_to_primary_and_fallback(fake):
    # with_fallbacks() returns a Runnable without bind_tools()/with_structured_output(), so those must be applied
    # to each model BEFORE the fallback wrapper: get_resilient_model(role, prepare=...) does that.
    fake.setenv("RELEASE_COPILOT_LLM__SUPERVISOR__MODEL", "fail-primary")
    fake.setenv("RELEASE_COPILOT_LLM__SUPERVISOR__FALLBACK", "fake-backup")
    seen = []

    def prepare(model):
        seen.append(model)
        return model.bind_tools([Verdict])

    reply = await get_resilient_model("supervisor", prepare=prepare).ainvoke("decide")
    assert [m.model_name for m in seen] == ["fail-primary", "fake-backup"]
    assert isinstance(reply, AIMessage)


def test_no_fallback_configured_returns_prepared_primary(fake):
    fake.setenv("RELEASE_COPILOT_LLM__WORKER__FALLBACK", "")  # env can't express None: empty string disables it
    assert isinstance(get_resilient_model("worker"), FakeChatModel)


# -------------------------------------------------------------------------------------- usage + cost


async def test_usage_is_tracked_per_role_and_model(fake):
    tracker = UsageTracker()
    config = {"callbacks": [tracker]}
    await get_chat_model("worker").ainvoke("a" * 400, config=config)  # 100 input tokens
    await get_chat_model("worker").ainvoke("b" * 400, config=config)
    await get_chat_model("judge").ainvoke("c" * 40, config=config)

    summary = tracker.summary()
    rows = {(r["role"], r["model"]): r for r in summary["by_role_model"]}
    s = load().llm
    worker = rows[("worker", s.worker.model)]
    assert worker["calls"] == 2 and worker["input_tokens"] == 200
    assert ("judge", s.judge.model) in rows
    assert summary["total_tokens"] == sum(r["input_tokens"] + r["output_tokens"] for r in rows.values())


async def test_fallback_calls_are_billed_to_the_fallback_model(fake):
    fake.setenv("RELEASE_COPILOT_LLM__WRITER__MODEL", "fail-primary")
    fake.setenv("RELEASE_COPILOT_LLM__WRITER__FALLBACK", "fake-backup")
    tracker = UsageTracker()
    await get_resilient_model("writer").ainvoke("x" * 400, config={"callbacks": [tracker]})
    [row] = tracker.summary()["by_role_model"]
    assert (row["role"], row["model"], row["calls"]) == ("writer", "fake-backup", 1)
    # fake pricing: $1 / 1M input, $2 / 1M output
    assert row["cost_usd"] == pytest.approx((row["input_tokens"] * 1 + row["output_tokens"] * 2) / 1e6)


def test_unknown_model_cost_is_unknown_not_zero():
    tracker = UsageTracker()
    tracker._add("worker", "some-new-model", 100, 10)
    assert tracker.total_cost_usd is None


def test_dated_model_names_match_pricing():
    assert price_for("gpt-4.1-mini-2025-04-14") == price_for("gpt-4.1-mini")
    assert price_for("gpt-4.1-2025-04-14") == price_for("gpt-4.1") != price_for("gpt-4.1-mini")


# ------------------------------------------------------------------------------- latency budget (review)


def test_primary_retries_less_when_a_fallback_exists(app_env):
    s = load().llm
    resilient = get_resilient_model("writer")  # writer has a fallback by default
    assert resilient.runnable.max_retries == s.retries_before_fallback < s.max_retries
    assert resilient.fallbacks[0].max_retries == s.max_retries
    assert get_chat_model("writer").max_retries == s.max_retries  # the plain primary keeps the full budget


def test_per_role_timeout_overrides_global(app_env):
    s = load().llm
    assert get_chat_model("writer").request_timeout == s.writer.timeout_seconds > s.timeout_seconds
    assert get_chat_model("worker").request_timeout == s.timeout_seconds


def test_default_judge_is_not_weaker_than_the_writer(app_env):
    s = load().llm
    assert s.judge.model not in {"gpt-4.1-nano", "gpt-4o-mini"}
    assert s.judge.temperature is None  # reasoning model
