"""What an evidence worker returns: the model's structured answer, and the system's report around it.

Two models on purpose:
- ``WorkerOutput`` is what the **model** fills in (``with_structured_output``): a summary and findings that cite
  evidence ids. It is judgment, so nothing in it is trusted yet.
- ``WorkerReport`` is what the **system** returns: the model's answer plus facts the model can't change: which
  tools ran, what failed (gaps), whether a budget ran out, and which cited ids don't exist in the evidence the
  worker actually collected ("facts in code, judgment in model").
"""

from typing import Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from release_copilot.evidence.index import EvidenceIndex
from release_copilot.evidence.model import EvidenceGap, ReleaseIdentity
from release_copilot.tools.base import ToolArtifact

Severity = Literal["info", "low", "medium", "high", "critical"]
WorkerStatus = Literal["complete", "exhausted", "no_answer"]
Budget = Literal["model_calls", "tool_calls"]


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=120, description="One line, e.g. 'PAY-104 is still In Progress'.")
    severity: Severity = Field(
        description="info: worth knowing, no risk · low, medium, high, critical: a release risk of that size."
    )
    detail: str = Field(max_length=800, description="What the evidence shows, in one to three sentences. Facts only.")
    evidence_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=20,
        description="The evidence ids this finding rests on, exactly as the tools returned them, e.g. jira:PAY-104.",
    )


class WorkerOutput(BaseModel):
    """A worker's final answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(min_length=1, max_length=1200, description="Three to five sentences on what the evidence shows.")
    findings: tuple[Finding, ...] = Field(default=(), max_length=25, description="Most severe first.")


class WorkerReport(BaseModel):
    """What a worker subgraph returns to its caller (the supervisor, A3)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    worker: str
    status: WorkerStatus = Field(
        description="complete: answered within budget · exhausted: a budget ran out (see gaps) · "
        "no_answer: the model's final answer was missing or invalid."
    )
    summary: str | None = None
    findings: tuple[Finding, ...] = ()
    gaps: tuple[EvidenceGap, ...] = ()
    unverified_citations: tuple[str, ...] = Field(
        default=(), description="Cited ids that are not in the evidence this worker collected. Never trust these."
    )
    model_calls: int
    tool_calls: int  # calls that reached a tool (not those refused for budget)
    artifacts: tuple[ToolArtifact, ...] = Field(default=(), repr=False)  # everything the tools returned, for A3/A4

    def index(self) -> EvidenceIndex:
        return EvidenceIndex.from_artifacts(self.artifacts)


class WorkerInput(TypedDict):
    """What a worker subgraph takes from its caller. Everything else in its state is private (context isolation)."""

    identity: ReleaseIdentity
    question: NotRequired[str | None]  # a supervisor follow-up (A3); None or absent: the worker's own review
    about: NotRequired[tuple[str, ...]]  # the evidence ids the follow-up is about


class WorkerResult(TypedDict):
    """What a worker subgraph gives back: only the report, never its messages."""

    report: WorkerReport
