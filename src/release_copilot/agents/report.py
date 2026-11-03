"""Turn a finished worker conversation into a ``WorkerReport``: the facts are read from the messages, not from the model.

Shared by both worker implementations (custom graph and prebuilt agent), so the two report the same way.
"""

from collections.abc import Sequence

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage

from release_copilot.agents.findings import Budget, WorkerOutput, WorkerReport
from release_copilot.agents.workers import WorkerSpec
from release_copilot.evidence.index import EvidenceIndex
from release_copilot.evidence.model import EvidenceGap
from release_copilot.tools.base import ToolArtifact


def tool_gaps(worker: str, messages: Sequence[AnyMessage]) -> list[EvidenceGap]:
    """The evidence a worker's tools failed to return, as gaps (YOUR PART).

    - Only tool messages that carry a ``ToolArtifact`` count: they reached a tool. A refused call (budget, unknown
      tool, invalid arguments) has no artifact and is not a gap here.
    - A failed artifact (``ok=False``) becomes a gap:
      * ``source``: the tool's domain, which is the tool name's prefix (``sonar_quality`` -> ``sonar``);
      * ``section``: ``f"{worker}.{tool}"``, e.g. ``quality.sonar_quality``;
      * ``subject``: the call's arguments, as ``"name=value"`` pairs sorted by name and joined by ``", "``
        (``"slot=staging"``), or None for a call without arguments. The arguments are in the ``AIMessage`` whose
        ``tool_calls`` has the ``ToolMessage``'s ``tool_call_id``;
      * ``kind``: ``"missing"`` for a ``NotFoundError``, otherwise ``"unavailable"``;
      * ``error_type``, ``message`` and ``retryable`` from the artifact's ``error``.
    - **A later success resolves the gap**: if the same tool is called again with the same arguments and succeeds,
      the earlier failure is not a gap (the model retried, and the evidence exists). A later failure of the same call
      replaces the earlier one (the newest error is what's true now).
    - Gaps are returned in the order their call first failed.
    """
    raise NotImplementedError("tool_gaps: your part of A2")


def build_report(
    spec: WorkerSpec,
    messages: Sequence[AnyMessage],
    *,
    output: WorkerOutput | None,
    model_calls: int,
    tool_calls: int,
    exhausted: Budget | None,
) -> WorkerReport:
    artifacts = tuple(m.artifact for m in messages if isinstance(m, ToolMessage) and isinstance(m.artifact, ToolArtifact))
    gaps = tool_gaps(spec.name, messages)
    if exhausted:
        limit = spec.max_model_calls if exhausted == "model_calls" else spec.max_tool_calls
        gaps.append(
            EvidenceGap(
                source="agent", section=spec.name, kind="exhausted", error_type="BudgetExhausted",
                message=f"the {spec.name} worker used all {limit} {exhausted.replace('_', ' ')} before it finished; "
                "its evidence may be incomplete",
            )
        )
    if output is None:
        gaps.append(
            EvidenceGap(
                source="agent", section=spec.name, kind="unavailable", error_type="InvalidOutput",
                message=f"the {spec.name} worker gave no valid final answer, so it has no findings",
            )
        )
    cited = [i for f in output.findings for i in f.evidence_ids] if output else []
    return WorkerReport(
        worker=spec.name,
        status="no_answer" if output is None else "exhausted" if exhausted else "complete",
        summary=output.summary if output else None,
        findings=output.findings if output else (),
        gaps=tuple(gaps),
        unverified_citations=tuple(EvidenceIndex.from_artifacts(artifacts).missing(cited)),
        model_calls=model_calls,
        tool_calls=tool_calls,
        artifacts=artifacts,
    )


def requested_tool_calls(messages: Sequence[AnyMessage], tools: Sequence[str]) -> int:
    """How many calls to the worker's own tools the model asked for (structured-answer calls don't count)."""
    return sum(1 for m in messages if isinstance(m, AIMessage) for c in m.tool_calls if c["name"] in tools)

