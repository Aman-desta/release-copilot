"""The citation validator: the gate between the writer's prose and anything stored or published.

A document can be fluent, well organised and wrong. The validator doesn't judge the writing (that is the judge's
job, O-stories); it checks what can be checked **deterministically**:

- every citation names something this run actually has (evidence, a known fact, a recorded gap);
- every sentence in a required section cites something;
- no sentence contradicts a fact computed in code (counts, versions, the gate, CVE ids);
- the risks section covers every risk the facts say it must.

Each failure is a ``ValidationIssue`` that names its section, so the revision can be **targeted**: the writer
rewrites only the sections that failed, with the exact problems listed, and the sections that passed are kept.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict

from release_copilot.documents.claims import (
    count_claims,
    cves_in,
    gate_claims,
    sentences,
    versions_in,
)
from release_copilot.documents.facts import DocumentContext
from release_copilot.documents.template import (
    CITED_SECTIONS,
    SECTION_KEYS,
    DocumentDraft,
)

IssueKind = Literal["empty_section", "unknown_citation", "uncited", "contradiction", "missing_risk"]


class ValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: IssueKind
    section: str  # a DocumentDraft field: summary, scope, ...
    detail: str  # what is wrong, written for the writer's revision
    sentence: str | None = None  # the sentence as written; None for section-level issues
    evidence_ids: tuple[str, ...] = ()  # the ids involved: the unknown citation, or the ids a missing risk accepts


class ValidationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    issues: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues

    def failed_sections(self) -> tuple[str, ...]:
        """The sections with at least one issue, in template order: what a revision rewrites."""
        failed = {i.section for i in self.issues}
        return tuple(k for k in SECTION_KEYS if k in failed)

    def kinds(self) -> set[tuple[str, str]]:
        """``{(kind, section)}``: a compact view for tests and logs."""
        return {(i.kind, i.section) for i in self.issues}


# ------------------------------------------------------------------------------------------- YOUR PART


def validate_document(draft: DocumentDraft, ctx: DocumentContext) -> ValidationReport:
    """Check a draft against the run's facts and evidence (YOUR PART). The contract is in tests/test_documents.py
    (the ``validate_document`` tests). Use the helpers in ``documents/claims.py``; don't write new regexes.

    For every section, in template order (``SECTION_KEYS``):

    1. **empty_section**: a section in ``CITED_SECTIONS`` whose body has no sentences. One issue; skip its other
       checks (there is nothing to check).
    2. Then for each sentence (``sentences(body)``), in order, at most one issue of each kind per sentence:
       - **unknown_citation** (every section): the cited ids not in ``ctx.known_ids``, plus every malformed
         citation (``sentence.malformed``, as written), sorted and unique, in ``evidence_ids``. A fake id is the
         worst failure: it *looks* verified. So does a malformed one, and the validator can't check it: fail closed.
       - **uncited** (sections in ``CITED_SECTIONS`` only): the sentence cites nothing at all (a malformed citation
         is an attempt to cite, reported as unknown_citation, not as uncited).
       - **contradiction** (every section), checked on ``sentence.claim`` (the text without citations, since ids
         like ``gh:compare:v2.3.1...release/2.4.0`` contain versions):
         * a count (``count_claims``) that differs from the fact it is about, **or** whose fact is unknown (None):
           stating an unknown number is inventing it;
         * a gate status (``gate_claims``) that differs from ``ctx.facts.gate_status``, or is stated while the
           gate is unknown;
         * a version (``versions_in``) not in ``ctx.known_versions``;
         * a CVE (``cves_in``) not in ``ctx.known_cves``.
         All the contradictions in one sentence go into one issue; ``detail`` says what each claim should be.
    3. **missing_risk** (the ``risks`` section, after its sentence issues): for each entry of
       ``ctx.must_cite_in_risks`` that no **clean** sentence in risks cites (any one id of the entry counts), one
       issue with that entry as ``evidence_ids``. Only citations in *risks* count: the CVE cited under quality isn't
       listed as a risk. A clean sentence has no issue of its own: a citation inside a contradicting sentence ("the
       gate passed, so it is not a risk [fact:gate_status]") doesn't list the risk, so both issues are reported and
       the revision knows it must fix the claim *and* keep the risk.

    Issues come out in that order: by section, then sentence order, then (unknown_citation, uncited,
    contradiction) within a sentence, then missing risks in ``must_cite_in_risks`` order.
    ``detail`` is free text for the writer; tests don't check its wording, but it must name what to fix.
    """
    issues: list[ValidationIssue] = []

    for section in SECTION_KEYS:
        body = getattr(draft, section)
        section_sentences = sentences(body)

        if section in CITED_SECTIONS and not section_sentences:
            issues.append(ValidationIssue(
                kind="empty_section",
                section=section,
                detail="Add content to this required section.",
            ))
            continue

        # Citations from sentences without any issue. Only these cover a required risk: an id inside a wrong
        # sentence ("the gate passed, not a risk [fact:gate_status]") doesn't list the risk.
        clean_citations: set[str] = set()
        for sentence in section_sentences:
            issues_before = len(issues)
            unknown = tuple(sorted((set(sentence.citations) - ctx.known_ids) | set(sentence.malformed)))
            if unknown:
                issues.append(ValidationIssue(
                    kind="unknown_citation",
                    section=section,
                    detail=f"Replace citations not present in this run: {', '.join(unknown)}.",
                    sentence=sentence.text,
                    evidence_ids=unknown,
                ))

            if section in CITED_SECTIONS and not sentence.citations and not sentence.malformed:
                issues.append(ValidationIssue(
                    kind="uncited",
                    section=section,
                    detail="Add at least one citation from this run.",
                    sentence=sentence.text,
                ))

            contradictions: list[str] = []
            for fact_name, value in count_claims(sentence.claim):
                expected = getattr(ctx.facts, fact_name)
                if expected is None:
                    contradictions.append(f"{value} {fact_name.replace('_', ' ')} is unknown")
                elif value != expected:
                    contradictions.append(f"{value} {fact_name.replace('_', ' ')} should be {expected}")

            for status in gate_claims(sentence.claim):
                expected = ctx.facts.gate_status
                if expected is None:
                    contradictions.append(f"quality gate status {status} is unknown")
                elif status != expected:
                    contradictions.append(f"quality gate status {status} should be {expected}")

            for version in versions_in(sentence.claim):
                if version not in ctx.known_versions:
                    contradictions.append(f"version {version} is not known in this run")

            for cve in cves_in(sentence.claim):
                if cve not in ctx.known_cves:
                    contradictions.append(f"CVE {cve} is not known in this run")

            if contradictions:
                issues.append(ValidationIssue(
                    kind="contradiction",
                    section=section,
                    detail="Correct these claims: " + "; ".join(contradictions) + ".",
                    sentence=sentence.text,
                ))

            if len(issues) == issues_before:
                clean_citations.update(sentence.citations)

        if section == "risks":
            for required_ids in ctx.must_cite_in_risks:
                if not clean_citations.intersection(required_ids):
                    issues.append(ValidationIssue(
                        kind="missing_risk",
                        section=section,
                        detail=f"List this risk in a correct, cited sentence; cite one of: {', '.join(required_ids)}.",
                        evidence_ids=required_ids,
                    ))

    return ValidationReport(issues=tuple(issues))