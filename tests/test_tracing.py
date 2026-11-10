"""O1: tracing. Spans go to an in-memory exporter through the same pipeline as production (the run-attribute
processor, then cost and sanitizing at export), so the tests assert what Phoenix and Langfuse would receive.

``run_attributes`` and ``sanitize_attributes`` (observability/tracing.py) are the spec; they're called through the
module so the processor, the exporter and the tests use the same functions.
"""

import asyncio
import base64
import json
import os
from collections.abc import Iterator
from types import MappingProxyType
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import SecretStr

from release_copilot.agents.workers import WORKERS, span_names
from release_copilot.config import TracingSettings, load_settings
from release_copilot.integrations.errors import UpstreamUnavailableError
from release_copilot.integrations.http import ServiceClient
from release_copilot.integrations.retry import RetryPolicy
from release_copilot.observability import tracing
from release_copilot.observability.redact import basic_redact
from release_copilot.observability.tracing import (
    AGENT,
    REDACTION_FAILED,
    RELEASE,
    RUN_ID,
    SCENARIO,
    THREAD_ID,
    TOOL,
    configure_tracing,
    hidden_marker,
    otlp_exporters,
)
from tests.integrations_support import FakeClock, Mocks
from tests.test_evidence import IDENTITY, collect
from tests.test_supervisor import RUN_ID as SUPERVISOR_RUN_ID
from tests.test_supervisor import Scripted, scripts_for, supervise

AGENTS, TOOLS = span_names()
RELEASE_ID = IDENTITY.correlation_id  # payments-api@2.4.0
PII = ("maria.gonzalez@example.com", "4111 1111 1111 1111", "078-05-1120", "(555) 014-2297")


# ------------------------------------------------------------------------------------------- helpers


class Traced:
    """Tracing on, exporting to memory, for the duration of a test."""

    def __init__(self, content: str = "redacted", redact=basic_redact) -> None:
        self.exporter = InMemorySpanExporter()
        settings = TracingSettings(enabled=False, content=content)  # type: ignore[arg-type]
        self.tracing = configure_tracing(settings, agents=AGENTS, tools=TOOLS, exporters=[self.exporter], redact=redact, batch=False)

    def spans(self) -> list[ReadableSpan]:
        return list(self.exporter.get_finished_spans())

    def shutdown(self) -> None:
        assert self.tracing is not None
        self.tracing.shutdown()


@pytest.fixture
def traced() -> Iterator[Traced]:
    t = Traced()
    yield t
    t.shutdown()


def A(span: ReadableSpan) -> dict[str, Any]:
    """A span's attributes as a plain dict (OTel types them as a union of every attribute value type)."""
    return dict(span.attributes or {})


def ids(span: ReadableSpan) -> tuple[int, int]:
    assert span.context is not None
    return span.context.trace_id, span.context.span_id


def run_trace(spans: list[ReadableSpan]) -> list[ReadableSpan]:
    """The spans of the one run in ``spans`` (the test's own reference collection traces separately)."""
    root = next(s for s in spans if s.name == "release_review")
    return [s for s in spans if ids(s)[0] == ids(root)[0]]


def parent_of(span: ReadableSpan, spans: list[ReadableSpan]) -> ReadableSpan | None:
    by_id = {ids(s)[1]: s for s in spans}
    return by_id.get(span.parent.span_id) if span.parent else None


def ancestors(span: ReadableSpan, spans: list[ReadableSpan]) -> list[str]:
    names, current = [], parent_of(span, spans)
    while current is not None:
        names.append(current.name)
        current = parent_of(current, spans)
    return names


def http_spans(spans: list[ReadableSpan]) -> list[ReadableSpan]:
    return [s for s in spans if "url.template" in (s.attributes or {})]


def everything_exported(spans: list[ReadableSpan]) -> str:
    """Every string a backend would receive: attributes, event attributes and status messages."""
    parts: list[str] = []
    for s in spans:
        parts += [s.name, s.status.description or "", json.dumps(dict(s.attributes or {}), default=str)]
        parts += [json.dumps(dict(e.attributes or {}), default=str) for e in s.events]
    return "\n".join(parts)


# ------------------------------------------------------------------------------ run_attributes (YOUR PART)


def attrs(**kw: Any) -> dict[str, Any]:
    names = {"run_id": RUN_ID, "thread_id": THREAD_ID, "release": RELEASE, "scenario": SCENARIO, "agent": AGENT, "tool": TOOL}
    return {names[k]: v for k, v in kw.items()}


def run_attributes(parent, name: str, own=None) -> dict[str, Any]:
    return tracing.run_attributes(parent, name=name, own=own or {}, agents=AGENTS, tools=TOOLS)


def test_run_attributes_copies_the_run_attributes_from_the_parent():
    parent = {**attrs(run_id="r1", thread_id="t1", release="svc@1", scenario="clean"), "input.value": "x", "llm.model_name": "m"}
    assert run_attributes(parent, "plan") == attrs(run_id="r1", thread_id="t1", release="svc@1", scenario="clean")


def test_run_attributes_without_a_parent_inherits_nothing():
    assert run_attributes(None, "plan") == {}
    assert run_attributes(None, "github_compare") == attrs(tool="github_compare")


def test_run_attributes_an_agent_span_names_its_agent_and_starts_outside_any_tool():
    parent = attrs(run_id="r1", agent="supervisor", tool="jira_get_issue")
    assert run_attributes(parent, "change_worker") == attrs(run_id="r1", agent="change")
    assert run_attributes(attrs(run_id="r1"), "supervisor") == attrs(run_id="r1", agent="supervisor")


def test_run_attributes_a_tool_span_names_its_tool_and_keeps_the_agent():
    assert run_attributes(attrs(run_id="r1", agent="change"), "github_compare") == attrs(run_id="r1", agent="change", tool="github_compare")
    # Below the tool (its HTTP requests), both are inherited.
    assert run_attributes(attrs(run_id="r1", agent="change", tool="github_compare"), "GET /repos") == attrs(run_id="r1", agent="change", tool="github_compare")


def test_run_attributes_never_overrides_what_the_span_already_has():
    parent = attrs(run_id="r1", thread_id="t1", agent="supervisor")
    own = attrs(run_id="r2", agent="custom")
    assert run_attributes(parent, "change_worker", own) == attrs(thread_id="t1")
    assert run_attributes(parent, "github_compare", attrs(tool="mine")) == attrs(run_id="r1", thread_id="t1", agent="supervisor")


def test_run_attributes_returns_a_new_dict_and_reads_its_inputs_only():
    parent = MappingProxyType(attrs(run_id="r1"))  # read-only: writing to it raises
    result = run_attributes(parent, "plan", MappingProxyType({}))
    result[RUN_ID] = "changed"
    assert parent[RUN_ID] == "r1"


# --------------------------------------------------------------------------- sanitize_attributes (YOUR PART)


def sanitize(attributes, content: str, redact=lambda s: s.replace("secret", "[x]")) -> dict[str, Any]:
    return tracing.sanitize_attributes(attributes, content=content, redact=redact)  # type: ignore[arg-type]


DESCRIPTIVE = {RUN_ID: "run secret 1", "llm.model_name": "gpt-secret", "llm.token_count.total": 12, "tool.name": "secret_tool"}


@pytest.mark.parametrize("content", ["raw", "redacted", "hidden"])
def test_sanitize_attributes_leaves_what_is_not_content_alone(content):
    assert sanitize(DESCRIPTIVE, content) == DESCRIPTIVE


def test_sanitize_attributes_raw_exports_content_unchanged():
    content = {"input.value": "a secret", "llm.input_messages.0.message.content": "secret"}
    assert sanitize(content, "raw") == content


def test_sanitize_attributes_redacted_runs_the_redactor_over_content():
    result = sanitize(
        {"input.value": "my secret", "llm.output_messages.0.message.content": "secret!", "llm.prompts": ["a secret", "b"], **DESCRIPTIVE},
        "redacted",
    )
    assert result == {"input.value": "my [x]", "llm.output_messages.0.message.content": "[x]!", "llm.prompts": ("a [x]", "b"), **DESCRIPTIVE}


def test_sanitize_attributes_hidden_keeps_only_the_size():
    result = sanitize({"output.value": "12345", "llm.prompts": ("abc", "de"), "exception.message": "", **DESCRIPTIVE}, "hidden")
    assert result == {"output.value": hidden_marker(5), "llm.prompts": hidden_marker(5), "exception.message": hidden_marker(0), **DESCRIPTIVE}


@pytest.mark.parametrize("content", ["redacted", "hidden"])
def test_sanitize_attributes_content_that_is_not_text_passes(content):
    assert sanitize({"input.value": 42, "output.value": True}, content) == {"input.value": 42, "output.value": True}


def test_sanitize_attributes_a_failing_redactor_never_leaks_the_raw_value():
    def redact(text: str) -> str:
        if "boom" in text:
            raise RuntimeError("redactor bug")
        return text.upper()

    result = sanitize({"input.value": "boom secret", "output.value": "fine", "llm.prompts": ("ok", "boom"), "tool.name": "t"}, "redacted", redact)
    assert result == {"input.value": REDACTION_FAILED, "output.value": "FINE", "llm.prompts": REDACTION_FAILED, "tool.name": "t"}


def test_sanitize_attributes_returns_a_new_dict_in_the_same_order():
    source = MappingProxyType({"tool.name": "t", "input.value": "secret", RUN_ID: "r", "output.value": "x"})
    result = sanitize(source, "redacted")
    assert list(result) == list(source) and result["input.value"] == "[x]"
    assert source["input.value"] == "secret"


def test_content_keys_are_classified():
    content = ["input.value", "output.value", "exception.message", "exception.stacktrace", "llm.prompts", "llm.prompts.0.prompt.text",
               "llm.input_messages.3.message.content", "llm.output_messages.0.message.contents.1.message_content.text",
               "llm.input_messages.2.message.tool_calls.0.tool_call.function.arguments"]
    descriptive = ["llm.input_messages.3.message.role", "llm.input_messages.2.message.tool_calls.0.tool_call.function.name",
                   "tool.name", "tool.description", "metadata", "llm.token_count.total", RUN_ID, "url.template"]
    assert all(tracing.is_content_key(k) for k in content)
    assert not any(tracing.is_content_key(k) for k in descriptive)


def test_the_basic_redactor_masks_the_mock_personal_data():
    text = ("Customer: Maria Gonzalez, maria.gonzalez@example.com, +1 (555) 014-2297. Card 4111 1111 1111 1111. "
            "SSN 078-05-1120. Order ORD-55120, PAY-102, epoch 1730000000000, at 2026-10-07T10:00:00.000+0000.")
    redacted = basic_redact(text)
    assert not any(p in redacted for p in PII)
    assert all(kept in redacted for kept in ("ORD-55120", "PAY-102", "1730000000000", "2026-10-07T10:00:00.000+0000"))
    # The Luhn check is a filter, not proof: about 1 in 10 long numbers pass it and get masked (a false positive).
    assert basic_redact("epoch 1730000000001") == "epoch [card]"


# ------------------------------------------------------------------------------- one run (acceptance)


async def test_one_run_is_one_trace_from_the_supervisor_down_to_http(mock_env, traced):
    scripted = Scripted(scripts_for(await collect(Mocks())))
    result = await supervise(Mocks(), scripted)
    spans = run_trace(traced.spans())
    root = next(s for s in spans if s.name == "release_review")

    assert root.parent is None and result.trace_id == format(ids(root)[0], "032x")
    names = {s.name for s in spans}
    assert {"supervisor", "collect", "plan", "worker", "review", "finalize"} <= names
    assert {f"{w}_worker" for w in WORKERS} <= names and set(TOOLS) <= names
    # Every span carries the run's identity, so any of them can be found by run, thread or release.
    expected = {RUN_ID: SUPERVISOR_RUN_ID, THREAD_ID: result.assessment.run.thread_id, RELEASE: RELEASE_ID}
    assert all({k: (s.attributes or {}).get(k) for k in expected} == expected for s in spans)
    assert not any(SCENARIO in (s.attributes or {}) for s in spans)  # no scenario in a real run


async def test_http_spans_nest_under_the_tool_that_made_them(mock_env, traced):
    scripted = Scripted(scripts_for(await collect(Mocks())))
    await supervise(Mocks(), scripted)
    spans = run_trace(traced.spans())
    requests = http_spans(spans)
    assert requests

    for span in requests:
        a, parent = span.attributes or {}, parent_of(span, spans)
        assert parent is not None
        if parent.name == "collect":  # the deterministic collector: the supervisor's, outside any tool
            assert a[AGENT] == "supervisor" and TOOL not in a
        else:  # a worker's tool call
            assert parent.name in TOOLS and a[TOOL] == parent.name
            worker = next(n for n in ancestors(span, spans) if n in AGENTS and n != "supervisor")
            assert a[AGENT] == AGENTS[worker]
    assert {A(s).get(AGENT) for s in requests} == {"supervisor", *WORKERS}
    template = next(s for s in requests if (s.attributes or {}).get(TOOL) == "github_compare")
    assert template.name.startswith("GET /repos/{owner}/{repo}/compare/") and "acme-fin" not in template.name  # templates, not ids


async def test_model_spans_carry_tokens_cost_and_their_agent(mock_env, traced):
    scripted = Scripted(scripts_for(await collect(Mocks())))
    result = await supervise(Mocks(), scripted)
    spans = run_trace(traced.spans())
    models = [s for s in spans if (s.attributes or {}).get("openinference.span.kind") == "LLM"]
    calls = sum(len(m.calls) for m in scripted.by_name.values())

    assert len(models) == calls
    assert {A(s)[AGENT] for s in models} == set(WORKERS)  # no supervisor call: nothing severe
    for s in models:
        a = A(s)
        assert a["llm.token_count.total"] == a["llm.token_count.prompt"] + a["llm.token_count.completion"] > 0
        expected = (a["llm.token_count.prompt"] * 1.00 + a["llm.token_count.completion"] * 2.00) / 1_000_000  # "fake" pricing
        assert a["llm.cost.total"] == a["gen_ai.usage.cost"] == pytest.approx(expected)
    # The root has the run's totals, the same numbers as the usage summary.
    root = next(s for s in spans if s.name == "release_review").attributes or {}
    assert root["release_copilot.usage.total_tokens"] == result.usage["total_tokens"] == sum(s.attributes["llm.token_count.total"] for s in models)  # type: ignore[index]
    assert root["release_copilot.usage.cost_usd"] == result.usage["total_cost_usd"]
    assert root["release_copilot.coverage"] == "complete" and root["release_copilot.findings"] == 4


async def test_a_scenario_and_a_session_reach_the_trace(mock_env, traced):
    scripted = Scripted(scripts_for(await collect(Mocks("critical_cve"))))
    await supervise(Mocks(), scripted, scenario="critical_cve", thread_id="thread-9")
    spans = run_trace(traced.spans())
    assert all((s.attributes or {}).get(SCENARIO) == "critical_cve" for s in spans)
    root = next(s for s in spans if s.name == "release_review").attributes or {}
    assert root["session.id"] == "thread-9" and root["langfuse.trace.metadata.scenario"] == "critical_cve"


async def test_retries_are_events_on_one_http_span(mock_env, traced):
    mocks = Mocks()
    mocks.fault("/jira", status=503)
    scripted = Scripted(scripts_for(await collect(Mocks())))
    await supervise(mocks, scripted)
    jira = [s for s in http_spans(run_trace(traced.spans())) if (s.attributes or {})["release_copilot.service"] == "jira"]

    assert jira and len(mocks.calls("/jira")) > len(jira)  # several attempts, one span each logical request
    for span in jira:
        a = span.attributes or {}
        retries = [e for e in span.events if e.name == "retry"]
        assert a["http.response.status_code"] == 503 and a["error.type"] == "UpstreamUnavailableError"
        assert a["http.request.resend_count"] == len(retries) > 0 and span.status.status_code == StatusCode.ERROR


async def test_a_direct_client_call_traces_outside_any_run(traced):
    replies = iter([httpx.Response(503), httpx.Response(200, json={"ok": True})])
    clock = FakeClock()
    client = ServiceClient("demo", "http://demo", transport=httpx.MockTransport(lambda r: next(replies)), sleep=clock.sleep, clock=clock)
    async with client:
        await client.get_json("/items/{id}", path_params={"id": "customer-42"})
    [span] = traced.spans()
    assert span.name == "GET /items/{id}" and span.parent is None  # the template: the id never reaches the trace
    assert span.attributes["http.response.status_code"] == 200 and span.attributes["http.request.resend_count"] == 1  # type: ignore[index]
    assert span.status.status_code == StatusCode.UNSET and [e.name for e in span.events] == ["retry"]


# ----------------------------------------------------------------------------------- personal data


async def run_pii_scenario(content: str) -> str:
    t = Traced(content)
    try:
        reference = await collect(Mocks("pii_in_ticket"))
        await supervise(Mocks("pii_in_ticket"), Scripted(scripts_for(reference)))
        return everything_exported(run_trace(t.spans()))
    finally:
        t.shutdown()


async def test_raw_traces_would_leak_the_tickets_personal_data(mock_env):
    exported = await run_pii_scenario("raw")
    assert all(p in exported for p in PII)  # the test can see a leak: otherwise the next test proves nothing


async def test_redacted_traces_leak_no_personal_data(mock_env):
    exported = await run_pii_scenario("redacted")
    assert not any(p in exported for p in PII)
    assert "[email]" in exported and "PAY-110" in exported  # redacted, not emptied: ids stay useful


async def test_hidden_traces_carry_no_content(mock_env):
    exported = await run_pii_scenario("hidden")
    assert not any(p in exported for p in PII) and "Customer charged twice" not in exported
    assert "[hidden: " in exported and "jira_get_issue" in exported  # the structure is all there


async def test_an_error_message_is_content_too(traced):
    client = ServiceClient("demo", "http://demo", transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"message": "bad email maria.gonzalez@example.com"})), retry=RetryPolicy(max_attempts=1))
    async with client:
        with pytest.raises(Exception, match="maria"):
            await client.get_json("/x")
    exported = everything_exported(traced.spans())
    assert "maria.gonzalez@example.com" not in exported and "[email]" in exported


# ---------------------------------------------------------------------------------------- set-up


def test_tracing_is_off_unless_enabled():
    assert configure_tracing(TracingSettings()) is None


def test_enabled_tracing_needs_an_exporter():
    with pytest.raises(ValueError, match="no exporter"):
        configure_tracing(TracingSettings(enabled=True))


def test_the_otlp_exporters_come_from_config():
    settings = TracingSettings(
        enabled=True, phoenix_endpoint="http://localhost:6006/v1/traces",  # type: ignore[arg-type]
        langfuse_endpoint="http://localhost:3000/api/public/otel/v1/traces",  # type: ignore[arg-type]
        langfuse_public_key="pk-lf-1", langfuse_secret_key="sk-lf-2",  # type: ignore[arg-type]
    )
    phoenix, langfuse = otlp_exporters(settings)
    assert phoenix._endpoint == "http://localhost:6006/v1/traces"  # type: ignore[attr-defined]
    assert langfuse._endpoint == "http://localhost:3000/api/public/otel/v1/traces"  # type: ignore[attr-defined]
    assert tracing.phoenix_headers(settings) == {}
    assert tracing.phoenix_headers(settings.model_copy(update={"phoenix_api_key": SecretStr("px-key")})) == {"Authorization": "Bearer px-key"}
    assert tracing.langfuse_headers(settings) == {
        "Authorization": "Basic " + base64.b64encode(b"pk-lf-1:sk-lf-2").decode(), "x-langfuse-ingestion-version": "4",
    }


def test_langfuse_needs_its_keys():
    with pytest.raises(ValueError, match="langfuse_public_key"):
        TracingSettings(langfuse_endpoint="http://localhost:3000/api/public/otel/v1/traces")  # type: ignore[arg-type]


def test_production_refuses_raw_trace_content(app_env):
    app_env.setenv("RELEASE_COPILOT_ENV", "prod")
    app_env.setenv("RELEASE_COPILOT_TRACING__CONTENT", "raw")
    with pytest.raises(Exception, match="raw content"):
        load_settings()


def test_langsmith_is_refused_in_production(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    with pytest.raises(ValueError, match="LANGSMITH_TRACING"):
        configure_tracing(TracingSettings(), env="prod")
    assert configure_tracing(TracingSettings(), env="local") is None  # allowed locally, with a warning


def test_without_tracing_a_run_has_no_trace_id():
    assert tracing.get_tracer().start_span("x").get_span_context().is_valid is False


def test_an_upstream_error_type_is_recorded(traced):
    span = tracing.get_tracer().start_span("x")
    tracing.record_error(span, UpstreamUnavailableError("down", service="svc"))
    span.end()
    [exported] = traced.spans()
    assert exported.attributes["error.type"] == "UpstreamUnavailableError" and exported.status.status_code == StatusCode.ERROR  # type: ignore[index]


# ----------------------------------------------------------------------------------------------- live


LOCAL_BACKENDS = {  # compose.observability.yaml's defaults; RELEASE_COPILOT_TRACING__* in the environment win
    "PHOENIX_ENDPOINT": "http://localhost:6006/v1/traces",
    "PHOENIX_API_KEY": "release-copilot-local-phoenix-admin-0001",
    "LANGFUSE_ENDPOINT": "http://localhost:3000/api/public/otel/v1/traces",
    "LANGFUSE_PUBLIC_KEY": "pk-lf-release-copilot-local",
    "LANGFUSE_SECRET_KEY": "sk-lf-release-copilot-local",
}


def live_tracing_settings() -> TracingSettings:
    values = {k: os.environ.get(f"RELEASE_COPILOT_TRACING__{k}", v) for k, v in LOCAL_BACKENDS.items()}
    return TracingSettings(enabled=True, **{k.lower(): v for k, v in values.items()})  # type: ignore[arg-type]


LIVE_TRACING = live_tracing_settings()  # read before the fixtures replace the environment


async def poll(fetch, done, *, seconds: float = 60.0):
    """Backends ingest asynchronously (Langfuse queues, then writes to ClickHouse): wait until ``done``."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        value = await fetch()
        if done(value) or asyncio.get_running_loop().time() > deadline:
            return value
        await asyncio.sleep(1.0)


@pytest.mark.live
async def test_live_one_run_reaches_phoenix_and_langfuse_with_the_same_ids(mock_env):
    """O1's acceptance against the real backends (``make obs-up``; run with ``--live``). Scripted models: no LLM cost."""
    settings, phoenix_base = LIVE_TRACING, str(LIVE_TRACING.phoenix_endpoint).removesuffix("/v1/traces")
    langfuse_base = str(LIVE_TRACING.langfuse_endpoint).removesuffix("/api/public/otel/v1/traces")
    auth = (settings.langfuse_public_key or "", settings.langfuse_secret_key.get_secret_value() if settings.langfuse_secret_key else "")
    phoenix_auth = tracing.phoenix_headers(settings)
    async with httpx.AsyncClient(timeout=5) as http:
        try:
            await http.get(f"{phoenix_base}/healthz")
            await http.get(f"{langfuse_base}/api/public/health")
        except httpx.TransportError:
            pytest.skip("tracing backends not running (make obs-up)")

        local = InMemorySpanExporter()  # what we sent, to compare with what each backend stored
        traced = configure_tracing(settings, agents=AGENTS, tools=TOOLS, exporters=[local])
        assert traced is not None
        try:
            scripted = Scripted(scripts_for(await collect(Mocks())))
            result = await supervise(Mocks(), scripted, scenario="clean")
        finally:
            traced.shutdown()  # flushes the batches to both backends
        sent = run_trace(list(local.get_finished_spans()))
        trace_id = result.trace_id
        assert trace_id is not None and len(sent) > 100

        async def phoenix_spans() -> list[dict[str, Any]]:
            spans, cursor = [], None
            while True:
                params = {"trace_id": trace_id, "limit": 1000, **({"cursor": cursor} if cursor else {})}
                response = await http.get(f"{phoenix_base}/v1/projects/{settings.service_name}/spans", params=params, headers=phoenix_auth)
                if response.status_code == 404:  # the project appears with its first span
                    return spans
                response.raise_for_status()
                page = response.json()
                spans += page.get("data", [])
                if not (cursor := page.get("next_cursor")):
                    return spans

        async def langfuse_observations() -> list[dict[str, Any]]:
            # Langfuse v4 (events-only): /api/public/v2/observations is the real-time read path; /traces is off.
            observations, cursor = [], None
            while True:
                params = {"traceId": trace_id, "limit": 1000, "fields": "core,basic,model,usage", **({"cursor": cursor} if cursor else {})}
                response = await http.get(f"{langfuse_base}/api/public/v2/observations", params=params, auth=auth)
                response.raise_for_status()
                page = response.json()
                observations += page.get("data", [])
                if not (cursor := page.get("meta", {}).get("cursor")):
                    return observations

        stored_phoenix = await poll(phoenix_spans, lambda s: len(s) >= len(sent))
        stored_langfuse = await poll(langfuse_observations, lambda o: len(o) >= len(sent))

    # The same trace id and the same span ids in both backends: one trace, two views.
    sent_ids = {format(ids(s)[1], "016x") for s in sent}
    assert {s["context"]["span_id"] for s in stored_phoenix} == sent_ids
    assert {s["context"]["trace_id"] for s in stored_phoenix} == {trace_id}
    assert {o["id"] for o in stored_langfuse} == sent_ids and {o["traceId"] for o in stored_langfuse} == {trace_id}
    root = next(s for s in stored_phoenix if s["name"] == "release_review")
    assert root["attributes"][RUN_ID] == SUPERVISOR_RUN_ID and root["attributes"][SCENARIO] == "clean"
    # Langfuse read the OpenInference model spans as generations, with their model, usage and our cost.
    generations = [o for o in stored_langfuse if o["type"] == "GENERATION"]
    assert len(generations) == len([s for s in sent if A(s).get("openinference.span.kind") == "LLM"])
    assert all(g["model"] == "fake-model" and g["totalCost"] > 0 for g in generations)
    print(f"\ntrace {trace_id}: {len(sent)} spans in Phoenix and Langfuse")
