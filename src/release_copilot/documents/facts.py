"""The deterministic facts a release document states, computed in code from the assessment.

**Facts in code, prose in the model.** Counts, versions, the gate status and the blocking alerts are not the
writer's to work out: code computes them once, the document shows them in a table the model can't edit, and the
validator checks the model's prose against them. Each known fact is citable as ``[fact:<name>]``, and each gap as
``[gap:<section>]``, so sentences like "no alert is open" or "coverage is unknown" can carry a citation too: an empty
list has no evidence id, and "unknown" has no evidence at all, but both are facts the run established.

A fact that is ``None`` is unknown (its evidence has a gap): it is **not** citable. Writing "the quality gate passed
[fact:gate_status]" when the gate is unknown is citing something that doesn't exist.
"""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from release_copilot.agents.assessment import ReleaseAssessment
from release_copilot.evidence.index import EvidenceIndex
from release_copilot.evidence.model import ReleaseEvidence

BLOCKING_SEVERITIES = ("critical", "high")
OK_CONCLUSIONS = ("success", "neutral", "skipped")
MIGRATION_PATH = re.compile(r"(^|/)Migrations/")  # EF Core migrations; the ModelSnapshot is a migration too
VERSION = re.compile(r"\bv?(\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)\b")
CVE = re.compile(r"\bCVE-\d{4}-\d{4,}\b")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BlockingAlert(_Frozen):
    """An open critical or high security alert: the document must list it under risks."""

    evidence_id: str  # gh:dependabot:34 or gh:code-scanning:12
    severity: str
    title: str  # the package or the rule
    cve_id: str | None = None


class ReleaseFacts(_Frozen):
    """What the document states as fact. ``None``: unknown (its evidence has a gap), so not citable."""

    release: str  # payments-api 2.4.0
    version: str
    previous_version: str
    coverage_status: str  # the evidence policy's verdict (A3): complete | partial | insufficient
    ticket_count: int | None = None
    open_tickets: tuple[str, ...] | None = None  # evidence ids of tickets not Done
    commit_count: int | None = None
    files_changed: int | None = None
    migrations: tuple[str, ...] | None = None  # changed files under Migrations/
    untracked_commits: tuple[str, ...] | None = None  # commit evidence ids that reached the branch without a PR
    failing_checks: tuple[str, ...] | None = None  # check evidence ids
    gate_status: str | None = None  # OK | ERROR | WARN | NONE
    blocking_alerts: tuple[BlockingAlert, ...] | None = None
    production_release: str | None = None  # what the production slot runs now
    staging_release: str | None = None

    def citable_ids(self) -> frozenset[str]:
        """``fact:<name>`` for every fact that is known."""
        return frozenset(f"fact:{name}" for name in type(self).model_fields if getattr(self, name) is not None)


class DocumentContext(_Frozen):
    """Everything the validator checks a document against. Built in code (``document_context``), never by a model."""

    facts: ReleaseFacts
    known_ids: frozenset[str]  # evidence ids (the index), fact ids and gap ids
    known_versions: frozenset[str]  # every version string that appears anywhere in the evidence
    known_cves: frozenset[str]  # every CVE id that appears in the evidence
    # Each entry is one risk the risks section must cite: any one of the ids in the entry will do.
    # A blocking alert: (its id,). The failing gate: (the gate's id, fact:gate_status). Built from the facts.
    must_cite_in_risks: tuple[tuple[str, ...], ...] = ()


# ----------------------------------------------------------------------------------------------- build


def release_facts(assessment: ReleaseAssessment) -> ReleaseFacts:
    ev, identity = assessment.evidence, assessment.identity
    issues, comparison, quality = ev.tickets.issues, ev.change.comparison, ev.quality
    return ReleaseFacts(
        release=f"{identity.service} {identity.version}",
        version=identity.version,
        previous_version=identity.previous_version,
        coverage_status=assessment.coverage.status,
        ticket_count=None if issues is None else len(issues),
        open_tickets=None if issues is None else tuple(i.evidence_id for i in issues if i.status_category != "done"),
        commit_count=None if comparison is None else comparison.total_commits,
        files_changed=None if comparison is None else len(comparison.files),
        migrations=None if comparison is None else tuple(f.filename for f in comparison.files if MIGRATION_PATH.search(f.filename)),
        untracked_commits=None if ev.change.commit_reviews is None else tuple(
            r.commit_id for r in ev.change.commit_reviews if r.pulls == ()
        ),
        failing_checks=None if quality.checks is None else tuple(
            c.evidence_id for c in quality.checks if c.conclusion not in OK_CONCLUSIONS
        ),
        gate_status=None if quality.gate is None else quality.gate.status,
        blocking_alerts=_blocking_alerts(ev),
        production_release=_slot_release(ev, "production"),
        staging_release=_slot_release(ev, "staging"),
    )


def _blocking_alerts(ev: ReleaseEvidence) -> tuple[BlockingAlert, ...] | None:
    deps, code = ev.quality.dependency_alerts, ev.quality.code_scanning_alerts
    if deps is None or code is None:  # unknown, not "none": one scan's silence can't vouch for the other's
        return None
    return (
        *(BlockingAlert(evidence_id=a.evidence_id, severity=a.severity, title=a.package, cve_id=a.cve_id)
          for a in deps if a.state == "open" and a.severity in BLOCKING_SEVERITIES),
        *(BlockingAlert(evidence_id=a.evidence_id, severity=a.severity, title=a.rule_id)
          for a in code if a.state == "open" and a.severity in BLOCKING_SEVERITIES),
    )


def _slot_release(ev: ReleaseEvidence, slot: str) -> str | None:
    return next((s.release for s in ev.deploy.slots or () if s.slot == slot), None)


def gap_id(section: str) -> str:
    return f"gap:{section}"


def document_context(assessment: ReleaseAssessment) -> DocumentContext:
    facts = release_facts(assessment)
    evidence_json = assessment.evidence.model_dump_json()
    risks: list[tuple[str, ...]] = [(a.evidence_id,) for a in facts.blocking_alerts or ()]
    if facts.gate_status == "ERROR" and assessment.evidence.quality.gate is not None:
        risks.append((assessment.evidence.quality.gate.evidence_id, "fact:gate_status"))
    risks += [(check, "fact:failing_checks") for check in facts.failing_checks or ()]
    risks += [(commit, "fact:untracked_commits") for commit in facts.untracked_commits or ()]
    return DocumentContext(
        facts=facts,
        known_ids=frozenset(EvidenceIndex.from_evidence(assessment.evidence).ids)
        | facts.citable_ids()
        | {gap_id(g.section) for g in assessment.all_gaps()},
        known_versions=frozenset(VERSION.findall(evidence_json)) | {facts.version, facts.previous_version},
        known_cves=frozenset(CVE.findall(evidence_json)),
        must_cite_in_risks=tuple(risks),
    )


# ----------------------------------------------------------------------------------------------- render


def facts_table(facts: ReleaseFacts) -> str:
    """The "Facts at a glance" table: rendered by code, so its numbers are right by construction."""

    def show(value: Any) -> str:
        if value is None:
            return "unknown"
        if isinstance(value, tuple):
            items = [v.evidence_id if isinstance(v, BlockingAlert) else str(v) for v in value]
            return ", ".join(f"`{i}`" for i in items) if items else "none"
        return str(value)

    rows = [
        ("Release", facts.release, "release"),
        ("Production runs", facts.production_release, "production_release"),
        ("Evidence coverage", facts.coverage_status, "coverage_status"),
        ("Tickets", facts.ticket_count, "ticket_count"),
        ("Tickets not done", facts.open_tickets, "open_tickets"),
        ("Commits", facts.commit_count, "commit_count"),
        ("Files changed", facts.files_changed, "files_changed"),
        ("Database migrations", facts.migrations, "migrations"),
        ("Commits without a PR", facts.untracked_commits, "untracked_commits"),
        ("Failing checks", facts.failing_checks, "failing_checks"),
        ("Quality gate", facts.gate_status, "gate_status"),
        ("Open critical or high alerts", facts.blocking_alerts, "blocking_alerts"),
    ]
    lines = ["| Fact | Value | Source |", "|---|---|---|"]
    lines += [f"| {label} | {show(value)} | `fact:{name}` |" for label, value, name in rows]
    return "\n".join(lines)
