"""The partial-evidence policy: which evidence a release can't be assessed without, and which it can.

A run never fails because a source is down (A1): the gap is recorded and the run goes on. This policy decides what
those gaps *mean*. Every evidence section is classified exactly once, in code, and reviewed like any other policy:

- **required**: without it nobody can say the release is safe (scope, what changed and who reviewed it, CI and
  security scans, the quality gate, where it will deploy, the service's CMDB record, and whether a change request
  already exists, which A6's idempotency depends on). A gap here makes the coverage ``insufficient``: a later
  step (A5) must not approve automatically;
- **optional**: useful context (coverage trends, deployment history, baseline metrics, past changes). A gap here
  makes the coverage ``partial``: the document says what's missing, and the run continues.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from release_copilot.agents.findings import WorkerStatus
from release_copilot.evidence.model import ReleaseEvidence

CoverageStatus = Literal["complete", "partial", "insufficient"]

SECTION_GROUPS = ("tickets", "change", "quality", "deploy", "service")
# Every section field of ReleaseEvidence, as the dotted paths the gaps use: tickets.issues, quality.gate, ...
SECTIONS: tuple[str, ...] = tuple(
    f"{group}.{name}"
    for group in SECTION_GROUPS
    for name in ReleaseEvidence.model_fields[group].annotation.model_fields  # type: ignore[union-attr]
)


def section_value(evidence: ReleaseEvidence, section: str) -> Any:
    """The value at a dotted section path: ``section_value(ev, "quality.gate")``. None means unknown (A1)."""
    group, name = section.split(".")
    return getattr(getattr(evidence, group), name)


@dataclass(frozen=True)
class EvidencePolicy:
    required: frozenset[str]
    optional: frozenset[str]

    def __post_init__(self) -> None:
        overlap = self.required & self.optional
        unknown = (self.required | self.optional) - set(SECTIONS)
        unclassified = set(SECTIONS) - self.required - self.optional
        if overlap or unknown or unclassified:
            raise ValueError(
                f"every evidence section must be classified exactly once: overlap={sorted(overlap)}, "
                f"unknown={sorted(unknown)}, unclassified={sorted(unclassified)}"
            )


DEFAULT_POLICY = EvidencePolicy(
    required=frozenset({
        "tickets.issues",
        "change.comparison",
        "change.commit_reviews",
        "quality.checks",
        "quality.dependency_alerts",
        "quality.code_scanning_alerts",
        "quality.gate",
        "deploy.slots",
        "service.ci",
        "service.existing_change",
    }),
    optional=frozenset({
        "quality.measures",
        "quality.previous_measures",
        "deploy.deployments",
        "deploy.metrics",
        "service.support_group",
        "service.recent_changes",
    }),
)


class Coverage(BaseModel):
    """How complete the run's evidence and judgment are, by the policy. Read by A4 (the document) and A5 (approval)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: CoverageStatus
    missing_required: tuple[str, ...] = ()  # sections, sorted
    missing_optional: tuple[str, ...] = ()  # sections, sorted
    incomplete_workers: tuple[str, ...] = ()  # workers, sorted: skipped, out of budget, or without a valid answer


# ------------------------------------------------------------------------------------------- YOUR PART


def assess_coverage(
    evidence: ReleaseEvidence,
    workers: Mapping[str, WorkerStatus | None],
    policy: EvidencePolicy = DEFAULT_POLICY,
) -> Coverage:
    """Apply the policy to the run's evidence gaps and worker outcomes (YOUR PART). The contract is in
    tests/test_supervisor.py (the ``assess_coverage`` tests).

    - The evidence gaps (``evidence.gaps``) decide which sections are missing. A section with *any* gap is missing,
      even a gap about one subject (one commit's PR lookup failed): part of the evidence is unknown, and the policy
      can't tell which part mattered.
    - A gap whose section the policy doesn't know counts as **required**: fail closed. A new evidence field added
      without a policy decision must make runs ``insufficient``, never silently pass.
    - ``workers`` maps every worker the run expected to the status of its first-round report, or None when it
      didn't run (skipped because its sources were down). Any worker that isn't ``"complete"`` is incomplete.
      Follow-up reports don't count here: a follow-up adds detail, but can't replace a worker's own review.
    - ``status``: ``insufficient`` if a required section is missing; otherwise ``partial`` if an optional section
      is missing or a worker is incomplete; otherwise ``complete``.
    - Every tuple in the result is sorted and has no duplicates.
    """
    missing_required: set[str] = set()
    missing_optional: set[str] = set()
    for gap in evidence.gaps:
        if gap.section in policy.optional:
            missing_optional.add(gap.section)
        else:
            missing_required.add(gap.section)  # required, or unknown to the policy: fail closed

    incomplete_workers: set[str] = {worker for worker, status in workers.items() if status != "complete"}

    if missing_required:
        status: CoverageStatus = "insufficient"
    elif missing_optional or incomplete_workers:
        status = "partial"
    else:
        status = "complete"

    return Coverage(
        status=status,
        missing_required=tuple(sorted(missing_required)),
        missing_optional=tuple(sorted(missing_optional)),
        incomplete_workers=tuple(sorted(incomplete_workers)),
    )
