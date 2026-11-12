"""The supervisor's result types: what A4 writes the document from, and what A5 decides on.

In their own module so the steps that read an assessment (documents, risk, change requests) can import them
without importing the supervisor graph, which in turn builds those steps.
"""

from pydantic import BaseModel, ConfigDict

from release_copilot.agents.findings import Finding, WorkerReport
from release_copilot.agents.followups import RejectedFollowUp, WorkerTask
from release_copilot.agents.policy import Coverage
from release_copilot.agents.run import RunInfo
from release_copilot.evidence.model import EvidenceGap, ReleaseEvidence, ReleaseIdentity

# ---------------------------------------------------------------------------------------------- results


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TaskReport(_Frozen):
    """A worker's report for one task, as the supervisor keeps it: evidence ids instead of raw tool artifacts.

    The artifacts hold every object the tools returned (personal data included) and don't survive a checkpoint
    round trip as objects, so the supervisor's state keeps only the ids, which is all it needs: the facts are in
    ``ReleaseEvidence``, and the ids are what citations and follow-ups are checked against.
    """

    task: WorkerTask
    report: WorkerReport
    collected: tuple[str, ...] = ()  # the evidence ids the worker's tools returned

    @classmethod
    def of(cls, task: WorkerTask, report: WorkerReport) -> "TaskReport":
        return cls(task=task, report=report.model_copy(update={"artifacts": ()}), collected=report.index().ids)


class AttributedFinding(_Frozen):
    worker: str
    round: int
    finding: Finding
    verified: bool  # every cited id is evidence this run collected; A4 refuses to cite an unverified finding


class ReleaseAssessment(_Frozen):
    """The supervisor's result: what A4 writes the document from, and what A5 decides on."""

    run: RunInfo
    identity: ReleaseIdentity
    evidence: ReleaseEvidence
    coverage: Coverage
    findings: tuple[AttributedFinding, ...]  # most severe first
    reports: tuple[TaskReport, ...]  # by round, then in worker order
    follow_ups: tuple[WorkerTask, ...] = ()
    rejected_follow_ups: tuple[RejectedFollowUp, ...] = ()
    gaps: tuple[EvidenceGap, ...] = ()  # the supervisor's own: skipped workers, spent budget, an invalid answer

    def all_gaps(self) -> tuple[EvidenceGap, ...]:
        return (*self.evidence.gaps, *self.gaps, *(g for r in self.reports for g in r.report.gaps))
