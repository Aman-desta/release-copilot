"""The document step: write, validate, revise once, store, publish.

    START -> write -> validate --ok--> store --valid--> publish -> END
                        |  ^                \\
                  issues|  |                 `--rejected--> END
                        v  |
                       revise  (once: only the failed sections are rewritten)

- **write**: the writer model fills a ``DocumentDraft`` from a brief built in code (``writer.writer_brief``).
- **validate**: ``validate_document``, deterministic. No model judges its own citations.
- **revise**: one targeted revision: the failed sections, with their issues, go back to the writer; only those
  sections are taken from the answer, so a revision can't break a section that passed.
- **store**: every document is stored, valid or rejected (``release_documents``, versioned).
- **publish**: a valid document becomes the release's draft on GitHub (``publish.ensure_draft_release``).

Failures are results, not crashes: an invalid answer twice is ``failed``, a document still invalid after the
revision is ``rejected`` (stored, not published), and a publish that GitHub or the rules refuse is
``unpublished`` with a gap. Only a bug raises.
"""

from typing import Any, Literal, TypedDict, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict

from release_copilot.agents.assessment import ReleaseAssessment
from release_copilot.agents.budget import BudgetExceeded
from release_copilot.agents.run import run_context
from release_copilot.documents import validate as validation
from release_copilot.documents.facts import DocumentContext, document_context
from release_copilot.documents.publish import PublishRefused, ensure_draft_release
from release_copilot.documents.store import DocumentRecord, DocumentStore, NewDocument
from release_copilot.documents.template import DocumentDraft, render
from release_copilot.documents.validate import ValidationReport
from release_copilot.documents.writer import (
    WRITER_PROMPT,
    apply_revision,
    revision_brief,
    writer_brief,
)
from release_copilot.evidence.index import EvidenceIndex
from release_copilot.evidence.model import EvidenceGap
from release_copilot.integrations.errors import IntegrationError
from release_copilot.integrations.github import GitHubClient
from release_copilot.integrations.models import Release

MAX_WRITER_CALLS = 2  # the draft, then one revision
DocumentOutcome = Literal["published", "unpublished", "rejected", "failed"]


class DocumentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: DocumentOutcome
    record: DocumentRecord | None = None  # None only when no draft could be written
    report: ValidationReport | None = None  # the final draft's validation
    first_report: ValidationReport | None = None  # the first draft's, when it was revised: what the revision fixed
    writer_calls: int = 0
    release: Release | None = None  # the GitHub draft, when published
    gaps: tuple[EvidenceGap, ...] = ()


class DocumentInput(TypedDict):
    assessment: ReleaseAssessment


class DocumentOutput(TypedDict):
    document: DocumentResult


class DocumentState(TypedDict):
    """Typed as required for the type checker; LangGraph fills the keys as the nodes write them."""

    assessment: ReleaseAssessment
    context: DocumentContext
    draft: DocumentDraft | None
    report: ValidationReport | None
    first_report: ValidationReport | None
    writer_calls: int
    record: DocumentRecord | None
    gaps: list[EvidenceGap]
    document: DocumentResult


def _gap(kind: Literal["unavailable", "exhausted", "blocked"], error_type: str, message: str, retryable: bool = False) -> EvidenceGap:
    return EvidenceGap(source="agent", section="document", kind=kind, error_type=error_type, message=message, retryable=retryable)


def build_document_graph(writer: BaseChatModel, store: DocumentStore, github: GitHubClient) -> CompiledStateGraph:
    structured = writer.with_structured_output(DocumentDraft, include_raw=True)

    async def ask(messages: list[BaseMessage], config: RunnableConfig) -> tuple[DocumentDraft | None, EvidenceGap | None]:
        try:
            result = cast(dict, await structured.ainvoke(messages, config))  # {"raw", "parsed", "parsing_error"}
        except BudgetExceeded as exc:
            return None, _gap("exhausted", "BudgetExhausted", str(exc))
        draft = result["parsed"]
        if draft is None or result["parsing_error"] is not None:
            return None, _gap("unavailable", "InvalidOutput", "the writer's answer was not a valid DocumentDraft")
        return draft, None

    def brief(state: DocumentState) -> list[BaseMessage]:
        return [SystemMessage(WRITER_PROMPT), HumanMessage(writer_brief(state["assessment"], state["context"]))]

    async def write(state: DocumentState, config: RunnableConfig) -> dict[str, Any]:
        ctx = document_context(state["assessment"])
        draft, gap = await ask(brief({**state, "context": ctx}), config)
        return {"context": ctx, "draft": draft, "writer_calls": 1, "gaps": [gap] if gap else []}

    async def validate(state: DocumentState) -> dict[str, Any]:
        draft = state.get("draft")
        if draft is None:
            return {"report": None}
        # Called through the module, so tests and the graph run the same function.
        return {"report": validation.validate_document(draft, state["context"])}

    def after_validate(state: DocumentState) -> Literal["revise", "store", "finish"]:
        report, calls = state.get("report"), state["writer_calls"]
        exhausted = any(g.kind == "exhausted" for g in state.get("gaps", ()))
        if report is not None and report.ok:
            return "store"
        if calls < MAX_WRITER_CALLS and not exhausted:
            return "revise"
        return "store" if report is not None else "finish"

    async def revise(state: DocumentState, config: RunnableConfig) -> dict[str, Any]:
        draft, report = state.get("draft"), state.get("report")
        messages = brief(state)
        if draft is not None and report is not None:
            messages.append(HumanMessage(revision_brief(draft, report)))
        revised, gap = await ask(messages, config)
        update: dict[str, Any] = {"writer_calls": state["writer_calls"] + 1, "first_report": report, "gaps": [*state.get("gaps", ()), *([gap] if gap else [])]}
        if revised is None:
            return update  # keep the first draft (if any): it is stored as rejected
        if draft is None or report is None:
            return {**update, "draft": revised}  # the first answer was invalid: this is the first real draft
        return {**update, "draft": apply_revision(draft, revised, report.failed_sections())}

    async def store_document(state: DocumentState, config: RunnableConfig) -> dict[str, Any]:
        draft, report, assessment = state["draft"], state["report"], state["assessment"]
        assert draft is not None and report is not None
        record = await store.save(NewDocument(
            release=assessment.identity.correlation_id,
            run_id=assessment.run.run_id,
            status="valid" if report.ok else "rejected",
            markdown=render(state["context"].facts, draft),
            evidence_fingerprint=EvidenceIndex.from_evidence(assessment.evidence).fingerprint(),
            issues=report.issues,
        ))
        return {"record": record}

    def after_store(state: DocumentState) -> Literal["publish", "finish"]:
        record = state.get("record")
        return "publish" if record is not None and record.status == "valid" else "finish"

    async def publish(state: DocumentState, config: RunnableConfig) -> dict[str, Any]:
        record = state["record"]
        assert record is not None
        with run_context(config):
            try:
                release = await ensure_draft_release(github, store, state["assessment"].identity, record)
            except PublishRefused as exc:
                return {"document": _result(state, "unpublished", gaps=[_gap("blocked", type(exc).__name__, str(exc))])}
            except IntegrationError as exc:
                gap = _gap("unavailable", type(exc).__name__, f"publishing the GitHub draft failed: {exc.message}", exc.retryable)
                return {"document": _result(state, "unpublished", gaps=[gap])}
        return {"document": _result(state, "published", release=release)}

    async def finish(state: DocumentState) -> dict[str, Any]:
        record = state.get("record")
        return {"document": _result(state, "failed" if record is None else "rejected")}

    graph = StateGraph(DocumentState, input_schema=DocumentInput, output_schema=DocumentOutput)
    graph.add_node("write", write)
    graph.add_node("validate", validate)
    graph.add_node("revise", revise)
    graph.add_node("store", store_document)
    graph.add_node("publish", publish)
    graph.add_node("finish", finish)
    graph.add_edge(START, "write")
    graph.add_edge("write", "validate")
    graph.add_conditional_edges("validate", after_validate)
    graph.add_edge("revise", "validate")
    graph.add_conditional_edges("store", after_store)
    graph.add_edge("publish", END)
    graph.add_edge("finish", END)
    return graph.compile(name="document")


def _result(state: DocumentState, status: DocumentOutcome, *, release: Release | None = None, gaps: list[EvidenceGap] | None = None) -> DocumentResult:
    return DocumentResult(
        status=status,
        record=state.get("record"),
        report=state.get("report"),
        first_report=state.get("first_report"),
        writer_calls=state.get("writer_calls", 0),
        release=release,
        gaps=(*state.get("gaps", ()), *(gaps or ())),
    )


async def write_release_document(
    graph: CompiledStateGraph, assessment: ReleaseAssessment, config: RunnableConfig | None = None
) -> DocumentResult:
    out = await graph.ainvoke(DocumentInput(assessment=assessment), config)
    return out["document"]
