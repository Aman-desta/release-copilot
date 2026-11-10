"""OpenTelemetry tracing: one trace per run, from the supervisor down to every HTTP request, sanitized before export.

    tracing = configure_tracing(settings.tracing, env=settings.env, agents=agents, tools=tools)
    ...                                   # runs: run_supervisor opens the root span
    tracing.shutdown()                    # flushes the batches and removes the instrumentation

Where the spans come from:

    release_review                     run_span() in run_supervisor: run_id, thread_id, release, scenario, totals
    └── supervisor                     OpenInference's LangChain instrumentation: every graph, node, model, tool
        ├── collect
        │   └── GET /rest/api/3/...    http_span() in ServiceClient.request: one span per logical request
        └── worker
            └── change_worker
                ├── agent → FakeChatModel / ChatOpenAI   (LLM spans: model, tokens; cost added at export)
                └── tools → github_compare
                            └── GET /repos/{owner}/{repo}/compare/{base}...{head}

Three pieces make that tree useful and safe:
- ``RunAttributeProcessor`` (``run_attributes``, YOUR PART) copies the run's attributes down the tree when each
  span starts, and names the agent and tool it belongs to, so any span, an HTTP request included, can be
  filtered by run, release, agent or tool;
- ``http_span`` bridges two context systems: OpenInference parents its spans through LangChain's run tree but
  doesn't make them the *current* OTel span, so an HTTP span asks LangChain which run it's in;
- ``SanitizingExporter`` (``sanitize_attributes``, YOUR PART) is the last code that sees a span before it
  leaves the process: it adds cost to model spans, then redacts or hides the content.

The OTLP exporters send the same spans to Phoenix and Langfuse, so one trace has the same id in both.
LangSmith is separate: LangChain sends to it directly when ``LANGSMITH_TRACING=true``, without our sanitizer.
"""

import base64
import logging
import os
import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from openinference.instrumentation.langchain import LangChainInstrumentor
from openinference.instrumentation.langchain import get_current_span as langchain_span
from openinference.semconv.trace import SpanAttributes
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import (
    Event,
    ReadableSpan,
    Span,
    SpanProcessor,
    TracerProvider,
)
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from release_copilot.config import ContentMode, TracingSettings
from release_copilot.llm.pricing import price_for
from release_copilot.observability.redact import Redactor, basic_redact

log = logging.getLogger(__name__)
TRACER_NAME = "release_copilot"

# The run's attributes: on the root span, then copied to every descendant (RunAttributeProcessor).
RUN_ID = "release_copilot.run_id"
THREAD_ID = "release_copilot.thread_id"
RELEASE = "release_copilot.release"  # e.g. payments-api@2.4.0
SCENARIO = "release_copilot.scenario"  # mock scenario, evals only; absent in real runs
AGENT = "release_copilot.agent"  # supervisor, jira, change, quality, deploy
TOOL = "release_copilot.tool"  # the tool a span runs inside, e.g. github_compare
INHERITED = (RUN_ID, THREAD_ID, RELEASE, SCENARIO, AGENT, TOOL)

# What counts as content: text that came from a prompt, a model, a tool or an exception. Everything else (names,
# ids, token counts, timings, status codes) describes the run and is exported as is.
_CONTENT_KEY = re.compile(
    r"input\.value|output\.value"
    r"|exception\.(?:message|stacktrace)"
    r"|llm\.prompts(?:\.\d+.*)?"
    r"|llm\.(?:input|output)_messages\.\d+\.message\."
    r"(?:content|contents\.\d+\.message_content\.text|tool_calls\.\d+\.tool_call\.function\.arguments)"
)
REDACTION_FAILED = "[redaction failed]"


def is_content_key(key: str) -> bool:
    return _CONTENT_KEY.fullmatch(key) is not None


def hidden_marker(chars: int) -> str:
    """What hidden content becomes: its size stays visible (useful for prompt-size problems), the text doesn't."""
    return f"[hidden: {chars} chars]"


# ------------------------------------------------------------------------------------------- YOUR PART


def run_attributes(
    parent: Mapping[str, Any] | None,
    *,
    name: str,
    own: Mapping[str, Any],
    agents: Mapping[str, str],
    tools: Collection[str],
) -> dict[str, AttributeValue]:
    """The attributes to add to a span as it starts (YOUR PART). The contract is in tests/test_tracing.py (the
    ``run_attributes`` tests); ``RunAttributeProcessor.on_start`` calls this for every span.

    - ``parent`` is the parent span's attributes, or None for a span without a (local) parent. Copy every
      ``INHERITED`` key the parent has.
    - A span named in ``agents`` belongs to that agent: set ``AGENT`` to ``agents[name]`` (e.g. ``change_worker`` ->
      ``change``). An agent starts outside any tool, so it does not inherit ``TOOL``.
    - A span named in ``tools`` is that tool's run: set ``TOOL`` to ``name``.
    - Never override what the span already has (``own``): leave those keys out of the result, so a root span's
      explicit attributes win over anything inherited.
    - Return only the attributes to add, as a new dict.
    """
    # First what the span should have, then one rule for precedence: a new rule can't forget it.
    result: dict[str, AttributeValue] = {k: parent[k] for k in INHERITED if k in parent} if parent else {}
    if name in agents:
        result.pop(TOOL, None)  # an agent starts outside any tool
        result[AGENT] = agents[name]
    if name in tools:
        result[TOOL] = name
    return {k: v for k, v in result.items() if k not in own}


def sanitize_attributes(
    attributes: Mapping[str, Any],
    *,
    content: ContentMode,
    redact: Redactor,
) -> dict[str, Any]:
    """The attributes as they may leave the process (YOUR PART). The contract is in tests/test_tracing.py (the
    ``sanitize_attributes`` tests); ``SanitizingExporter`` calls this for every span and every span event.

    - Keys that aren't content (``is_content_key``) are returned unchanged.
    - ``raw``: content is unchanged too.
    - ``redacted``: a string becomes ``redact(value)``; a sequence of strings becomes a tuple of redacted strings.
    - ``hidden``: a string becomes ``hidden_marker(len(value))``; a sequence of strings becomes one marker with
      their total length.
    - Any other content value (a number, a bool) is returned unchanged.
    - If ``redact`` raises, that value becomes ``REDACTION_FAILED``: never the raw text, and the other attributes
      are still sanitized (one bad value must not stop the export, or leak).
    - Return a new dict in the same key order; don't modify ``attributes``.
    """
    result: dict[str, Any] = {}
    for key, value in attributes.items():
        if not is_content_key(key):
            result[key] = value
            continue

        if content == "raw":
            result[key] = value
            continue

        if isinstance(value, str):
            values = [value]
        elif isinstance(value, Sequence) and all(isinstance(v, str) for v in value):
            values = value
        else:
            result[key] = value
            continue

        try:
            if content == "redacted":
                sanitized = tuple(redact(v) for v in values)
                result[key] = sanitized[0] if isinstance(value, str) else sanitized
            else:
                result[key] = hidden_marker(sum(len(v) for v in values))
        except Exception as exc:  # noqa: BLE001 (content must fail closed if redaction fails)
            log.warning("redact() failed on %s: %s", key, type(exc).__name__)
            result[key] = REDACTION_FAILED

    return result


# ---------------------------------------------------------------------------------------- processors


class RunAttributeProcessor(SpanProcessor):
    """Runs ``run_attributes`` for every span as it starts.

    It has to be ``on_start``: a span is read-only once it ends, and its children start while it is still open,
    so this is the moment the parent's attributes are available to copy.
    """

    def __init__(self, agents: Mapping[str, str], tools: Collection[str]) -> None:
        self.agents = dict(agents)
        self.tools = frozenset(tools)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        parent = trace.get_current_span(parent_context)
        parent_attributes = getattr(parent, "attributes", None) if parent.get_span_context().is_valid else None
        span.set_attributes(
            run_attributes(parent_attributes, name=span.name, own=span.attributes or {}, agents=self.agents, tools=self.tools)
        )


def with_cost(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Model spans get their cost in USD, from llm/pricing.py. Unknown pricing adds nothing: unknown is not 0.

    Both conventions: OpenInference's ``llm.cost.*`` (Phoenix) and ``gen_ai.usage.cost`` (Langfuse).
    """
    result = dict(attributes)
    model = attributes.get(SpanAttributes.LLM_MODEL_NAME)
    price = price_for(model) if isinstance(model, str) else None
    if price is None:
        return result
    prompt = int(attributes.get(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, 0)) * price.input_per_million / 1_000_000
    completion = int(attributes.get(SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, 0)) * price.output_per_million / 1_000_000
    result[SpanAttributes.LLM_COST_PROMPT] = round(prompt, 8)
    result[SpanAttributes.LLM_COST_COMPLETION] = round(completion, 8)
    result[SpanAttributes.LLM_COST_TOTAL] = result["gen_ai.usage.cost"] = round(prompt + completion, 8)
    return result


class SanitizingExporter(SpanExporter):
    """Wraps a real exporter: every span is enriched with cost, then sanitized, then exported.

    Export time is the last moment, so it covers everything: OpenInference's spans, ours, and anything a library
    adds later. Span events (exceptions) and the error status message are content too.
    """

    def __init__(self, inner: SpanExporter, *, content: ContentMode, redact: Redactor = basic_redact) -> None:
        self.inner = inner
        self.content: ContentMode = content
        self.redact = redact

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([self._sanitize(span) for span in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)

    def _clean(self, attributes: Mapping[str, Any] | None) -> dict[str, Any]:
        return sanitize_attributes(attributes or {}, content=self.content, redact=self.redact)

    def _sanitize(self, span: ReadableSpan) -> ReadableSpan:
        status = span.status
        if status.description:
            description = self._clean({"exception.message": status.description})["exception.message"]
            status = Status(status.status_code, description)
        return ReadableSpan(
            name=span.name,
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=self._clean(with_cost(span.attributes or {})),
            events=[Event(e.name, self._clean(e.attributes), e.timestamp) for e in span.events],
            links=span.links,
            kind=span.kind,
            status=status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )


# ------------------------------------------------------------------------------------------- spans


@dataclass
class _State:
    provider: TracerProvider | None = None


_state = _State()


def get_tracer() -> trace.Tracer:
    """The configured tracer, or a no-op one: code can always open spans, and they cost nothing when tracing is off."""
    return _state.provider.get_tracer(TRACER_NAME) if _state.provider else trace.NoOpTracer()


@contextmanager
def run_span(*, run_id: str, thread_id: str, release: str, scenario: str | None) -> Iterator[trace.Span]:
    """The run's root span. Everything the run does nests under it, because it is the current span while the graph
    runs and OpenInference parents its root on the current span."""
    attributes: dict[str, AttributeValue] = {
        RUN_ID: run_id, THREAD_ID: thread_id, RELEASE: release,
        SpanAttributes.SESSION_ID: thread_id,  # Phoenix and Langfuse group a session's traces by it
        "langfuse.trace.name": "release_review",
        "langfuse.trace.metadata.run_id": run_id,
        "langfuse.trace.metadata.release": release,
    }
    if scenario is not None:
        attributes[SCENARIO] = attributes["langfuse.trace.metadata.scenario"] = scenario
    with get_tracer().start_as_current_span("release_review", attributes=attributes) as span:
        yield span


def _http_parent() -> Context | None:
    # OpenInference doesn't make its spans current, so inside a tool the current OTel span is still the run's
    # root. LangChain's run context knows the innermost run (the tool, or the node): parent on that first.
    span = langchain_span()
    return trace.set_span_in_context(span) if span is not None else None


@contextmanager
def http_span(service: str, method: str, path_template: str) -> Iterator[trace.Span]:
    """One CLIENT span per logical request (retries are events on it). The name and attributes use the path
    *template*: ids and query strings can carry personal data, and templates keep the span names few."""
    attributes: dict[str, AttributeValue] = {
        "http.request.method": method, "url.template": path_template, "release_copilot.service": service,
    }
    with get_tracer().start_as_current_span(
        f"{method} {path_template}", context=_http_parent(), kind=SpanKind.CLIENT, attributes=attributes
    ) as span:
        yield span


def trace_id_of(span: trace.Span) -> str | None:
    context = span.get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


# ------------------------------------------------------------------------------------------- setup


@dataclass
class Tracing:
    provider: TracerProvider
    instrumentor: LangChainInstrumentor

    def force_flush(self) -> bool:
        return self.provider.force_flush()

    def shutdown(self) -> None:
        self.instrumentor.uninstrument()
        self.provider.shutdown()  # flushes the batches first
        if _state.provider is self.provider:
            _state.provider = None


def langfuse_headers(settings: TracingSettings) -> dict[str, str]:
    """Langfuse's OTLP endpoint takes Basic auth with the project's key pair (it supports OTLP over HTTP only)."""
    assert settings.langfuse_public_key and settings.langfuse_secret_key  # checked by TracingSettings
    token = f"{settings.langfuse_public_key}:{settings.langfuse_secret_key.get_secret_value()}"
    return {
        "Authorization": "Basic " + base64.b64encode(token.encode()).decode(),
        "x-langfuse-ingestion-version": "4",  # Langfuse: show new data in real time
    }


def phoenix_headers(settings: TracingSettings) -> dict[str, str]:
    """Phoenix with auth on (its default outside loopback) takes a bearer API key; without one, no header."""
    key = settings.phoenix_api_key
    return {"Authorization": f"Bearer {key.get_secret_value()}"} if key is not None else {}


def otlp_exporters(settings: TracingSettings) -> list[SpanExporter]:
    exporters: list[SpanExporter] = []
    if settings.phoenix_endpoint is not None:
        exporters.append(OTLPSpanExporter(endpoint=str(settings.phoenix_endpoint), headers=phoenix_headers(settings)))
    if settings.langfuse_endpoint is not None:
        exporters.append(OTLPSpanExporter(endpoint=str(settings.langfuse_endpoint), headers=langfuse_headers(settings)))
    return exporters


def check_langsmith(env: str) -> None:
    """LangSmith gets LangChain's callbacks directly: prompts and tool output, unsanitized. Not in production."""
    if os.environ.get("LANGSMITH_TRACING", "").lower() != "true":
        return
    if env == "prod":
        raise ValueError("LANGSMITH_TRACING sends unsanitized prompts and tool output: it can't be on in production")
    log.warning("LangSmith tracing is on: it receives unsanitized content (local development only)")


def configure_tracing(
    settings: TracingSettings,
    *,
    env: str = "local",
    agents: Mapping[str, str] | None = None,
    tools: Collection[str] = (),
    exporters: Sequence[SpanExporter] = (),
    redact: Redactor = basic_redact,
    batch: bool = True,
    set_global: bool = False,
) -> Tracing | None:
    """Set up tracing, or return None when it is off. ``exporters`` adds exporters (tests use an in-memory one);
    ``batch=False`` exports each span as it ends (tests); ``set_global`` also makes this the process-wide
    provider, for libraries that trace on their own (the CLI and, later, the API service)."""
    check_langsmith(env)
    all_exporters = [*otlp_exporters(settings), *exporters] if settings.enabled else list(exporters)
    if not all_exporters:
        if settings.enabled:
            raise ValueError("tracing is enabled but no exporter is configured (tracing.phoenix_endpoint or tracing.langfuse_endpoint)")
        return None
    provider = TracerProvider(resource=Resource.create({
        "service.name": settings.service_name,
        "deployment.environment.name": env,  # Langfuse files traces under this environment
        "openinference.project.name": settings.service_name,  # Phoenix files traces under this project
    }))
    provider.add_span_processor(RunAttributeProcessor(agents or {}, tools))
    for exporter in all_exporters:
        safe = SanitizingExporter(exporter, content=settings.content, redact=redact)
        provider.add_span_processor(BatchSpanProcessor(safe) if batch else SimpleSpanProcessor(safe))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    if set_global:
        trace.set_tracer_provider(provider)
    _state.provider = provider
    return Tracing(provider, instrumentor)


def record_error(span: trace.Span, error: BaseException) -> None:
    """Mark a span failed with the error's type; its message goes through the sanitizer like any content."""
    span.set_attribute("error.type", type(error).__name__)
    span.set_status(Status(StatusCode.ERROR, str(error)))
