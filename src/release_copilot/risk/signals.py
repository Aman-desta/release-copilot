"""Risk signals: the facts the change policy decides on, computed in code from the assessment.

Each signal is a value plus the evidence ids it rests on (what a blocker cites). ``value=None`` means **unknown**:
the evidence behind it has a gap, and the gap ids are its evidence. An empty tuple means "checked, none".

Only one signal comes from model judgment: ``severe_findings``, the workers' *verified* high or critical findings.
It can only add risk (a rule adds points for it), so a model can raise the risk through it but never lower it.
"""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from release_copilot.agents.assessment import ReleaseAssessment
from release_copilot.documents.facts import ReleaseFacts, gap_id, release_facts

_CRITICALITY = re.compile(r"^\s*(\d+)")  # ServiceNow business_criticality: "1 - most critical"


class Signal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Any = None  # None: unknown
    evidence_ids: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        return self.value is not None


class RiskSignals(BaseModel):
    """The closed set of signals a policy rule can name. Adding one is a code change, reviewed like any other."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    open_blocking_alerts: Signal  # tuple of alert ids (critical or high, open)
    quality_gate_failed: Signal  # bool
    failing_checks: Signal  # tuple of check ids
    untracked_commits: Signal  # tuple of commit ids pushed without a PR
    db_migrations: Signal  # tuple of migration files
    ci_criticality: Signal  # int: the CMDB business criticality, 1 = most critical
    evidence_coverage: Signal  # "complete" | "partial" | "insufficient" (A3 policy); always known
    open_tickets: Signal  # tuple of ticket ids not Done
    severe_findings: Signal  # tuple of evidence ids behind verified high or critical findings; always known

    def get(self, name: str) -> Signal:
        return getattr(self, name)


def _known(value: Any, *ids: str) -> Signal:
    return Signal(value=value, evidence_ids=tuple(ids))


def _unknown(assessment: ReleaseAssessment, *sections: str) -> Signal:
    gaps = {gap_id(g.section) for g in assessment.all_gaps() if any(g.section == s or g.section.startswith(f"{s}.") for s in sections)}
    return Signal(value=None, evidence_ids=tuple(sorted(gaps)))


def risk_signals(assessment: ReleaseAssessment, facts: ReleaseFacts | None = None) -> RiskSignals:
    facts = facts or release_facts(assessment)
    ev, a = assessment.evidence, assessment

    def tuple_signal(value: tuple[str, ...] | None, *sections: str) -> Signal:
        return _unknown(a, *sections) if value is None else _known(value, *value)

    gate = ev.quality.gate
    ci = ev.service.ci
    criticality = _CRITICALITY.match(ci.business_criticality) if ci is not None else None
    severe = tuple(sorted({i for f in a.findings if f.verified and f.finding.severity in ("high", "critical") for i in f.finding.evidence_ids}))
    coverage = a.coverage
    return RiskSignals(
        open_blocking_alerts=_unknown(a, "quality.dependency_alerts", "quality.code_scanning_alerts")
        if facts.blocking_alerts is None
        else _known(tuple(b.evidence_id for b in facts.blocking_alerts), *(b.evidence_id for b in facts.blocking_alerts)),
        quality_gate_failed=_unknown(a, "quality.gate") if gate is None else _known(gate.status == "ERROR", gate.evidence_id),
        failing_checks=tuple_signal(facts.failing_checks, "quality.checks"),
        untracked_commits=tuple_signal(facts.untracked_commits, "change.commit_reviews"),
        db_migrations=_unknown(a, "change.comparison") if facts.migrations is None else _known(facts.migrations, "fact:migrations"),
        # An empty or unparsable criticality is unknown, not "not critical": the policy decides what that means.
        ci_criticality=_unknown(a, "service.ci") if criticality is None else _known(int(criticality.group(1)), ci.evidence_id),  # type: ignore[union-attr]
        evidence_coverage=_known(coverage.status, *(gap_id(s) for s in (*coverage.missing_required, *coverage.missing_optional))),
        open_tickets=tuple_signal(facts.open_tickets, "tickets.issues"),
        severe_findings=_known(severe, *severe),
    )
