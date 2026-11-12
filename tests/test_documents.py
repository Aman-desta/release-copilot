"""A4: the release document. ``validate_document`` (documents/validate.py) is YOUR PART; its contract is the
``validate_document`` section below, checked against a small hand-built context so each rule is visible.

The rest is the framework around it: the facts computed in code, the claim helpers, the template, the writer's
briefs, the versioned store (in memory and Postgres), publishing the GitHub draft, and the document step end to end
inside the supervisor, with a scripted writer. ``draft_for`` is that writer: a model that follows the rules, built
from the evidence, so "every scenario passes validation" tests the validator and the facts, not a model's luck.
"""

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from release_copilot.agents.assessment import ReleaseAssessment
from release_copilot.documents import validate as validation
from release_copilot.documents.claims import (
    count_claims,
    gate_claims,
    sentences,
    versions_in,
)
from release_copilot.documents.facts import (
    BlockingAlert,
    DocumentContext,
    ReleaseFacts,
    document_context,
    gap_id,
    release_facts,
)
from release_copilot.documents.graph import DocumentResult
from release_copilot.documents.publish import (
    NotLatestVersion,
    ReleaseAlreadyPublished,
    ensure_draft_release,
)
from release_copilot.documents.store import (
    InMemoryDocumentStore,
    NewDocument,
    PostgresDocumentStore,
)
from release_copilot.documents.template import (
    CITED_SECTIONS,
    SECTION_KEYS,
    DocumentDraft,
    render,
)
from release_copilot.documents.validate import ValidationIssue, ValidationReport
from release_copilot.documents.writer import (
    apply_revision,
    revision_brief,
    writer_brief,
)
from release_copilot.evidence.model import QualitySection
from release_copilot.llm.fake import FakeChatModel
from tests.integrations_support import Mocks
from tests.test_evidence import IDENTITY, SCENARIOS, collect, toolbox
from tests.test_supervisor import RUN_ID, Scripted, scripts_for

CVE = "CVE-2026-41922"
GATE = "sonar:gate:release/2.4.0"


# ======================================================================================================
# validate_document (YOUR PART): a hand-built context, one rule per test
# ======================================================================================================

CTX = DocumentContext(
    facts=ReleaseFacts(
        release="payments-api 2.4.0", version="2.4.0", previous_version="2.3.1", coverage_status="complete",
        ticket_count=8, open_tickets=(), commit_count=9, files_changed=20, migrations=(), untracked_commits=(),
        failing_checks=(), gate_status="ERROR",
        blocking_alerts=(BlockingAlert(evidence_id="gh:dependabot:34", severity="critical", title="Acme.Shared.Serialization", cve_id=CVE),),
        production_release="2.3.1", staging_release="2.4.0-rc.1",
    ),
    known_ids=frozenset({
        "jira:PAY-101", "jira:PAY-102", "gh:pr:411", "gh:pr:412", "gh:dependabot:34", GATE, "azure:slot:production",
        "azure:slot:staging", "gh:compare:v2.3.1...release/2.4.0", "fact:commit_count", "fact:ticket_count",
        "fact:gate_status", "fact:blocking_alerts", "gap:deploy.metrics",
    }),
    known_versions=frozenset({"2.4.0", "2.3.1", "2.4.0-rc.1"}),
    known_cves=frozenset({CVE, "CVE-2026-40117"}),
    must_cite_in_risks=(("gh:dependabot:34",), (GATE, "fact:gate_status")),
)

VALID = DocumentDraft(
    summary="Release 2.4.0 adds Apple Pay and idempotent capture. It is blocked by a critical alert.",
    scope="- PAY-101 adds Apple Pay [jira:PAY-101].\n- PAY-102 makes capture idempotent [jira:PAY-102].",
    changes="The release contains 9 commits [fact:commit_count] [gh:compare:v2.3.1...release/2.4.0]. "
    "Checkout changed in PR 411 [gh:pr:411].",
    quality=f"The quality gate failed [{GATE}]. Dependency alert 34 is critical ({CVE}) [gh:dependabot:34].",
    deployment="Production runs 2.3.1 [azure:slot:production]. Staging runs 2.4.0-rc.1 [azure:slot:staging].",
    risks=f"- {CVE} in Acme.Shared.Serialization is critical [gh:dependabot:34].\n- The quality gate fails [fact:gate_status].",
    rollback="Swap the slots back so production runs 2.3.1 again.",
)


def with_section(**sections: str) -> DocumentDraft:
    return VALID.model_copy(update=sections)


def validate(draft: DocumentDraft, ctx: DocumentContext = CTX) -> ValidationReport:
    return validation.validate_document(draft, ctx)


def only(report: ValidationReport) -> ValidationIssue:
    assert len(report.issues) == 1, report.issues
    return report.issues[0]


def test_validate_a_valid_draft_has_no_issues():
    report = validate(VALID)
    assert report.ok and report.issues == ()


def test_validate_citations_written_after_the_full_stop_count():
    assert validate(with_section(scope="PAY-101 adds Apple Pay. [jira:PAY-101]")).ok


def test_validate_an_unknown_citation_is_caught_in_every_section():
    for section in SECTION_KEYS:
        body = getattr(VALID, section) + " Settlement is faster [jira:PAY-999] [gh:pr:9999] [jira:PAY-999]."
        issue = only(validate(with_section(**{section: body})))
        assert (issue.kind, issue.section) == ("unknown_citation", section)
        assert issue.evidence_ids == ("gh:pr:9999", "jira:PAY-999")  # sorted, once each
        assert issue.sentence is not None and "Settlement is faster" in issue.sentence


@pytest.mark.parametrize(
    ("section", "bracket"),
    [("summary", "[jira: PAY-999]"), ("summary", "[JIRA:PAY-101]"), ("rollback", "[confluence:RB-12]"), ("scope", "[jira: PAY-101]")],
)
def test_validate_a_malformed_citation_is_unknown_in_every_section(section, bracket):
    """Found in the A4 review: these rendered like checked citations but were read as prose, so in summary and
    rollback they passed. Anything citation-shaped that isn't a valid citation fails closed."""
    issue = only(validate(with_section(**{section: f"It fixes a fraud bug {bracket}."})))
    assert (issue.kind, issue.section, issue.evidence_ids) == ("unknown_citation", section, (bracket,))


def test_validate_a_markdown_link_is_not_a_citation():
    assert validate(with_section(summary="See [the runbook](https://example.com/runbook) for details.")).ok


def test_validate_a_fact_or_gap_id_must_be_known_too():
    issue = only(validate(with_section(deployment=VALID.deployment + " Error rates are unknown [gap:quality.measures].")))
    assert issue.kind == "unknown_citation" and issue.evidence_ids == ("gap:quality.measures",)
    assert validate(with_section(deployment=VALID.deployment + " Error rates are unknown [gap:deploy.metrics].")).ok


def test_validate_an_uncited_sentence_fails_only_in_the_cited_sections():
    for section in SECTION_KEYS:
        report = validate(with_section(**{section: getattr(VALID, section) + " Everything else is fine."}))
        if section in CITED_SECTIONS:
            issue = only(report)
            assert (issue.kind, issue.section, issue.sentence) == ("uncited", section, "Everything else is fine.")
        else:
            assert report.ok, section


def test_validate_an_empty_cited_section_is_one_issue():
    issue = only(validate(with_section(scope="  \n\n ")))
    assert (issue.kind, issue.section, issue.sentence) == ("empty_section", "scope", None)
    assert validate(with_section(summary="")).ok  # not a cited section: empty is allowed


def test_validate_an_empty_risks_section_reports_only_that():
    issue = only(validate(with_section(risks="")))  # no missing_risk issues on top: there is nothing to check
    assert (issue.kind, issue.section) == ("empty_section", "risks")


def test_validate_a_count_that_contradicts_a_fact():
    issue = only(validate(with_section(changes="The release contains 12 commits [fact:commit_count].")))
    assert (issue.kind, issue.section) == ("contradiction", "changes")
    assert validate(with_section(changes="The release contains nine commits [fact:commit_count].")).ok
    assert validate(with_section(changes="One untracked commit was found [fact:commit_count].")).ok  # not a total


def test_validate_a_count_of_an_unknown_fact_is_a_contradiction():
    ctx = CTX.model_copy(update={"facts": CTX.facts.model_copy(update={"files_changed": None})})
    issue = only(validate(with_section(changes=VALID.changes + " It touches 20 files [gh:pr:411]."), ctx))
    assert issue.kind == "contradiction"


def test_validate_a_gate_status_that_contradicts_the_gate():
    issue = only(validate(with_section(quality=f"The quality gate passed [{GATE}].")))
    assert (issue.kind, issue.section) == ("contradiction", "quality")
    unknown = CTX.model_copy(update={"facts": CTX.facts.model_copy(update={"gate_status": None})})
    report = validate(with_section(quality=f"The quality gate failed [{GATE}]."), unknown)
    # risks says it fails too: a contradiction there, so that sentence no longer covers the gate risk either
    assert report.kinds() == {("contradiction", "quality"), ("contradiction", "risks"), ("missing_risk", "risks")}


def test_validate_an_invented_version_or_cve():
    assert only(validate(with_section(deployment="Production runs 2.2.9 [azure:slot:production]."))).kind == "contradiction"
    assert only(validate(with_section(summary="It fixes CVE-2026-99999."))).kind == "contradiction"  # every section


def test_validate_versions_inside_citation_ids_are_not_claims():
    ctx = CTX.model_copy(update={"known_versions": frozenset({"2.4.0", "2.3.1", "2.4.0-rc.1"})})
    body = "The diff is against production [gh:compare:v2.3.1...release/2.4.0]."
    assert validate(with_section(changes=body), ctx).ok
    odd = ctx.model_copy(update={"known_ids": ctx.known_ids | {"gh:compare:v9.9.9...release/2.4.0"}})
    assert validate(with_section(changes="The diff is against production [gh:compare:v9.9.9...release/2.4.0]."), odd).ok


def test_validate_all_contradictions_in_a_sentence_are_one_issue():
    body = f"The release contains 12 commits, the quality gate passed and production runs 2.2.9 [{GATE}] [fact:commit_count]."
    issue = only(validate(with_section(changes=body)))
    assert issue.kind == "contradiction"
    for expected in ("12", "2.2.9"):
        assert expected in issue.detail  # the detail names what is wrong, so the revision can fix it


def test_validate_a_sentence_can_have_one_issue_of_each_kind():
    report = validate(with_section(changes="The release contains 12 commits [jira:PAY-999]."))
    assert [i.kind for i in report.issues] == ["unknown_citation", "contradiction"]  # cited, so not "uncited"


def test_validate_a_blocking_alert_must_be_cited_under_risks():
    issue = only(validate(with_section(risks="- The quality gate fails [fact:gate_status].")))
    assert (issue.kind, issue.section, issue.evidence_ids) == ("missing_risk", "risks", ("gh:dependabot:34",))
    # Cited under quality (VALID.quality cites it) doesn't count: it isn't listed as a risk.


def test_validate_any_id_of_a_risk_entry_satisfies_it():
    by_gate = f"- {CVE} is critical [gh:dependabot:34].\n- The quality gate fails [{GATE}]."
    assert validate(with_section(risks=by_gate)).ok
    missing = only(validate(with_section(risks=f"- {CVE} is critical [gh:dependabot:34].")))
    assert missing.kind == "missing_risk" and missing.evidence_ids == (GATE, "fact:gate_status")


def test_validate_a_risk_cited_only_in_a_wrong_sentence_is_still_missing():
    """A citation counts for a risk only in a sentence without issues: here the gate's id sits in a sentence that
    contradicts the gate, so the risk is reported missing too, and the revision must fix the claim and keep the risk."""
    risks = f"- {CVE} is critical [gh:dependabot:34].\n- The quality gate passed, so it is not a risk [fact:gate_status]."
    report = validate(with_section(risks=risks))
    assert [(i.kind, i.evidence_ids) for i in report.issues] == [
        ("contradiction", ()),
        ("missing_risk", (GATE, "fact:gate_status")),
    ]
    unknown = f"- {CVE} is critical [gh:dependabot:34] [jira:PAY-999].\n- The quality gate fails [fact:gate_status]."
    assert [i.kind for i in validate(with_section(risks=unknown)).issues] == ["unknown_citation", "missing_risk"]


def test_validate_issues_come_in_section_then_sentence_order():
    draft = with_section(
        risks="Unrelated [jira:PAY-999]. Nothing cited here.",
        scope="No citation. Another [jira:PAY-998].",
        summary="It fixes CVE-2026-99999.",
    )
    report = validate(draft)
    assert [(i.section, i.kind) for i in report.issues] == [
        ("summary", "contradiction"),
        ("scope", "uncited"),
        ("scope", "unknown_citation"),
        ("risks", "unknown_citation"),
        ("risks", "uncited"),
        ("risks", "missing_risk"),
        ("risks", "missing_risk"),
    ]
    assert report.failed_sections() == ("summary", "scope", "risks")


# ======================================================================================================
# The claim helpers (mine)
# ======================================================================================================


def test_sentences_split_lines_items_and_sentences_and_keep_citations():
    found = sentences("- A [jira:PAY-1]. B, e.g. C [gh:pr:1].\n\n1. D. [gh:pr:2] [gh:pr:3]\nE")
    assert [(s.claim, s.citations) for s in found] == [
        ("A .", ("jira:PAY-1",)),
        ("B, e.g. C .", ("gh:pr:1",)),
        ("D.", ("gh:pr:2", "gh:pr:3")),
        ("E", ()),
    ]


def test_count_claims_match_totals_only():
    assert count_claims("The release contains 8 commits across 20 changed files; all eight Jira tickets.") == [
        ("commit_count", 8), ("files_changed", 20), ("ticket_count", 8),
    ]
    assert count_claims("1 ticket is not done and 2 commits have no PR") == []


def test_gate_and_version_claims():
    assert gate_claims("The quality gate failed, the quality gate status is OK.") == ["ERROR", "OK"]
    assert versions_in("Staging runs 2.4.0-rc.1. Production runs v2.3.1.") == ["2.4.0-rc.1", "2.3.1"]


# ======================================================================================================
# Facts, template and briefs (mine)
# ======================================================================================================


async def assessment_for(scenario: str = "clean", mocks: Mocks | None = None) -> ReleaseAssessment:
    from tests.test_supervisor import supervise

    reference = await collect(Mocks(scenario))
    return (await supervise(mocks or Mocks(scenario), Scripted(scripts_for(reference)))).assessment


async def test_facts_for_critical_cve(mock_env):
    a = await assessment_for("critical_cve")
    facts = release_facts(a)
    assert (facts.ticket_count, facts.commit_count, facts.files_changed, facts.gate_status) == (8, 8, 20, "ERROR")
    assert [(b.evidence_id, b.cve_id) for b in facts.blocking_alerts or ()] == [("gh:dependabot:34", CVE), ("gh:code-scanning:12", None)]
    assert facts.failing_checks == ("gh:check:CodeQL@" + a.evidence.quality.checks[3].head_sha[:7],)  # type: ignore[index]
    assert facts.migrations and all("Migrations/" in f for f in facts.migrations)
    ctx = document_context(a)
    assert ctx.must_cite_in_risks[:3] == (("gh:dependabot:34",), ("gh:code-scanning:12",), (GATE, "fact:gate_status"))
    assert CVE in ctx.known_cves and "2.4.0-rc.1" in ctx.known_versions
    assert {"jira:PAY-101", "fact:commit_count"} <= ctx.known_ids


async def test_unknown_facts_are_none_and_not_citable(mock_env):
    a = await assessment_for()
    unknown = a.model_copy(update={"evidence": a.evidence.model_copy(update={"quality": QualitySection()})})
    facts = release_facts(unknown)
    assert facts.gate_status is None and facts.blocking_alerts is None and facts.failing_checks is None
    ids = document_context(unknown).known_ids
    assert "fact:gate_status" not in ids and "fact:commit_count" in ids


async def test_a_gap_is_citable_by_its_section(mock_env):
    mocks = Mocks()
    mocks.fault("/sonar", status=503)
    a = await assessment_for("clean", mocks)
    ctx = document_context(a)
    assert gap_id("quality.gate") in ctx.known_ids and ctx.facts.gate_status is None


def test_render_puts_the_facts_table_and_every_section_in_order():
    markdown = render(CTX.facts, VALID)
    headings = [line for line in markdown.splitlines() if line.startswith("## ")]
    assert headings == ["## Facts at a glance", "## Summary", "## Scope", "## Changes by area", "## Quality and security",
                        "## Deployment and migration notes", "## Risks", "## Rollback summary"]
    assert "| Commits | 9 | `fact:commit_count` |" in markdown
    assert "| Open critical or high alerts | `gh:dependabot:34` | `fact:blocking_alerts` |" in markdown


async def test_the_writer_brief_has_ids_and_facts_but_no_ticket_text(mock_env):
    a = await assessment_for("pii_in_ticket")
    brief = writer_brief(a, document_context(a))
    assert "[jira:PAY-110]" in brief and "[fact:commit_count] 9" in brief and "<evidence>" in brief
    for issue in a.evidence.tickets.issues or ():  # descriptions and comments never reach the writer
        if issue.description_markdown:
            assert issue.description_markdown[:40] not in brief
        for comment in issue.comments:
            assert comment.body_markdown[:40] not in brief


async def test_the_writer_brief_leaves_out_unverified_findings(mock_env):
    a = await assessment_for()
    first = a.findings[0]
    hidden = first.model_copy(update={"verified": False, "finding": first.finding.model_copy(update={"title": "A rumour"})})
    a = a.model_copy(update={"findings": (hidden, *a.findings[1:])})
    assert "A rumour" not in writer_brief(a, document_context(a))


def test_the_revision_brief_and_apply_revision_touch_only_failed_sections():
    report = validate(with_section(scope="No citation."))
    brief = revision_brief(with_section(scope="No citation."), report)
    assert "## scope" in brief and "No citation." in brief and "## summary" not in brief
    revised = DocumentDraft(**{k: f"rewritten {k} [jira:PAY-101]." for k in SECTION_KEYS})
    merged = apply_revision(VALID, revised, report.failed_sections())
    assert merged.scope == "rewritten scope [jira:PAY-101]." and merged.summary == VALID.summary


# ======================================================================================================
# The store: one contract, two implementations
# ======================================================================================================

DB_URL = os.environ.get("RELEASE_COPILOT_TEST_DATABASE_URL")
DB_SCHEMA = os.environ.get("RELEASE_COPILOT_DB_SCHEMA", "copilot")


@pytest.fixture(params=["memory", "postgres"])
def store(request):
    if request.param == "memory":
        return InMemoryDocumentStore()
    if not DB_URL:
        pytest.skip("RELEASE_COPILOT_TEST_DATABASE_URL not set")
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.attributes.update(database_url=DB_URL, db_schema=DB_SCHEMA)
    command.upgrade(cfg, "head")
    return PostgresDocumentStore(DB_URL.replace("postgresql+psycopg://", "postgresql://"), DB_SCHEMA)


def new_doc(release: str, markdown: str = "# notes", *, status: str = "valid", fingerprint: str = "f" * 64, run_id: str = "run-1") -> NewDocument:
    issues = () if status == "valid" else (ValidationIssue(kind="uncited", section="scope", detail="cite it", sentence="X."),)
    return NewDocument(release=release, run_id=run_id, status=status, markdown=markdown, evidence_fingerprint=fingerprint, issues=issues)  # type: ignore[arg-type]


async def test_store_versions_count_up_per_release(store):
    release = f"svc-{uuid.uuid4().hex[:8]}@1.0.0"
    first = await store.save(new_doc(release, "v1"))
    second = await store.save(new_doc(release, "v2", status="rejected"))
    other = await store.save(new_doc(f"other-{release}", "v1"))
    assert (first.version, second.version, other.version) == (1, 2, 1)
    assert second.issues[0].kind == "uncited"  # issues survive the round trip
    assert [r.version for r in await store.versions(release)] == [1, 2]
    assert (await store.latest(release)).version == 2  # type: ignore[union-attr]
    assert (await store.latest(release, status="valid")).version == 1  # type: ignore[union-attr]
    assert await store.latest("nothing@0.0.0") is None


async def test_store_saving_the_same_content_again_returns_the_same_version(store):
    release = f"svc-{uuid.uuid4().hex[:8]}@1.0.0"
    first = await store.save(new_doc(release, "same", run_id="run-1"))
    again = await store.save(new_doc(release, "same", run_id="run-2"))
    assert again == first and again.run_id == "run-1"
    new_evidence = await store.save(new_doc(release, "same", fingerprint="0" * 64))
    assert new_evidence.version == 2  # the same words over different evidence are a new version


async def test_store_concurrent_saves_get_distinct_versions(store):
    release = f"svc-{uuid.uuid4().hex[:8]}@1.0.0"
    records = await asyncio.gather(*(store.save(new_doc(release, f"doc {i}")) for i in range(6)))
    assert sorted(r.version for r in records) == [1, 2, 3, 4, 5, 6]
    same = await asyncio.gather(*(store.save(new_doc(release, "dup")) for _ in range(4)))
    assert len({r.version for r in same}) == 1


# ======================================================================================================
# Publishing the GitHub draft
# ======================================================================================================


async def published(box, store, markdown: str):
    record = await store.save(new_doc(IDENTITY.correlation_id, markdown))
    return record, await ensure_draft_release(box.github, store, IDENTITY, record)


async def test_publish_creates_a_draft_then_updates_its_notes(mock_env):
    mocks, store = Mocks(), InMemoryDocumentStore()
    async with toolbox(mocks) as box:
        _, first = await published(box, store, "# first")
        _, second = await published(box, store, "# second")
        _, third = await published(box, store, "# second")  # unchanged: no write at all
        releases = [r for r in await box.github.list_releases() if r.tag_name == "v2.4.0"]
    assert first.draft and first.body == "# first" and second.id == first.id
    assert len(releases) == 1 and releases[0].body == "# second" and releases[0].draft
    assert third == second
    patches = [c for c in mocks.calls("/github") if c["method"] == "PATCH"]
    assert len(patches) == 1


async def test_publish_never_touches_a_published_release(mock_env):
    mocks, store = Mocks(), InMemoryDocumentStore()
    async with toolbox(mocks) as box:
        _, draft = await published(box, store, "# first")
        await box.github._http.request("PATCH", f"/repos/{{owner}}/{{repo}}/releases/{draft.id}", path_params=box.github._repo, json={"draft": False})
        with pytest.raises(ReleaseAlreadyPublished):
            await published(box, store, "# rewritten")
        body = next(r.body for r in await box.github.list_releases() if r.tag_name == "v2.4.0")
    assert body == "# first"


async def test_publish_only_the_latest_valid_version(mock_env):
    store = InMemoryDocumentStore()
    async with toolbox(Mocks()) as box:
        old = await store.save(new_doc(IDENTITY.correlation_id, "# old"))
        await store.save(new_doc(IDENTITY.correlation_id, "# new"))
        with pytest.raises(NotLatestVersion):
            await ensure_draft_release(box.github, store, IDENTITY, old)


# ======================================================================================================
# End to end: the document step in the supervisor (acceptance)
# ======================================================================================================


def _risk_line(ctx: DocumentContext, ids: tuple[str, ...]) -> str:
    alert = next((a for a in ctx.facts.blocking_alerts or () if a.evidence_id == ids[0]), None)
    if alert is not None:
        cve = f" ({alert.cve_id})" if alert.cve_id else ""
        return f"- A {alert.severity} alert in {alert.title}{cve} is open and must be fixed first [{alert.evidence_id}]."
    if "fact:gate_status" in ids:
        return f"- The quality gate fails [{ids[0]}]."
    return f"- {ids[0]} needs attention before release [{ids[0]}]."


def draft_for(a: ReleaseAssessment, ctx: DocumentContext) -> DocumentDraft:
    """A writer that follows the rules, using the evidence as the oracle."""
    ev, f = a.evidence, ctx.facts
    scope = [f"- {i.key} is a {i.issue_type.lower()} in status {i.status} [{i.evidence_id}]." for i in ev.tickets.issues or ()]
    changes = []
    if (cmp := ev.change.comparison) is not None:
        changes.append(f"The release contains {f.commit_count} commits across {f.files_changed} files [fact:commit_count] [fact:files_changed] [{cmp.evidence_id}].")
    for r in ev.change.commit_reviews or ():
        changes += [f"- PR {p.pull.number} brings commit {r.sha[:7]} [{p.pull.evidence_id}] [{r.commit_id}]." for p in r.pulls or ()]
        if r.pulls == ():
            changes.append(f"- Commit {r.sha[:7]} reached the branch without a pull request [{r.commit_id}].")
    q = ev.quality
    quality = [f"- Check {c.name} concluded {c.conclusion} [{c.evidence_id}]." for c in q.checks or ()]
    if q.gate is not None:
        quality.append(f"The quality gate status is {q.gate.status} [{q.gate.evidence_id}].")
    quality += [f"- Dependency alert {d.number} is {d.severity} in {d.package} ({d.cve_id}) [{d.evidence_id}]." for d in q.dependency_alerts or ()]
    quality += [f"- Code scanning alert {c.number} is {c.severity} [{c.evidence_id}]." for c in q.code_scanning_alerts or ()]
    deployment = [f"The {s.slot} slot runs {s.release} [{s.evidence_id}]." for s in ev.deploy.slots or ()]
    deployment.append(f"Database migrations: {', '.join(f.migrations or ()) or 'none'} [fact:migrations].")
    risks = [_risk_line(ctx, ids) for ids in ctx.must_cite_in_risks]
    risks += [f"- Evidence is missing for {g.section} [{gap_id(g.section)}]." for g in a.all_gaps()]
    if not risks:
        risks = ["No open critical or high alert was found [fact:blocking_alerts]."]
    return DocumentDraft(
        summary=f"{f.release} replaces {f.previous_version} in production.",
        scope="\n".join(scope), changes="\n".join(changes), quality="\n".join(quality),
        deployment="\n".join(deployment), risks="\n".join(risks),
        rollback=f"Swap the slots back so production runs {f.previous_version} again.",
    )


def as_answer(draft: DocumentDraft | dict[str, Any]) -> AIMessage:
    args = draft.model_dump() if isinstance(draft, DocumentDraft) else draft
    return AIMessage(content="", tool_calls=[{"name": "DocumentDraft", "args": args, "id": "draft"}])


async def run_with_writer(scenario: str, replies: list[Any], *, store=None, mocks: Mocks | None = None, writer=None):
    from release_copilot.agents.supervisor import build_supervisor, run_supervisor

    reference = await collect(Mocks(scenario))
    scripted = Scripted(scripts_for(reference))
    scripted.by_name["writer"] = writer or FakeChatModel(responses=replies)
    store = store or InMemoryDocumentStore()
    mocks = mocks or Mocks(scenario)
    async with toolbox(mocks) as box:
        graph = build_supervisor(box, scripted.models, documents=store)
        result = await run_supervisor(graph, IDENTITY, run_id=RUN_ID)
        releases = await box.github.list_releases()
    assert result.document is not None
    return result, result.document, scripted["writer"], releases


async def oracle_draft(scenario: str) -> DocumentDraft:
    a = await assessment_for(scenario)
    return draft_for(a, document_context(a))


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_every_scenario_produces_a_valid_published_document(mock_env, scenario):
    draft = await oracle_draft(scenario)
    result, doc, writer, releases = await run_with_writer(scenario, [as_answer(draft)])
    assert doc.status == "published" and doc.writer_calls == 1 and doc.report is not None and doc.report.ok, doc.report
    assert doc.record is not None and doc.record.status == "valid" and doc.record.version == 1
    draft_release = next(r for r in releases if r.tag_name == "v2.4.0")
    assert draft_release.draft and draft_release.body == doc.record.markdown == doc.release.body  # type: ignore[union-attr]
    assert len(writer.calls) == 1 and result.trace_id is None


async def test_the_critical_cve_is_listed_under_risks(mock_env):
    _, doc, _, _ = await run_with_writer("critical_cve", [as_answer(await oracle_draft("critical_cve"))])
    markdown = doc.record.markdown  # type: ignore[union-attr]
    risks = markdown.split("## Risks", 1)[1].split("## ", 1)[0]
    assert CVE in risks and "[gh:dependabot:34]" in risks


async def test_a_draft_that_leaves_the_cve_out_of_risks_is_revised(mock_env):
    good = await oracle_draft("critical_cve")
    no_cve = good.model_copy(update={"risks": "\n".join(l for l in good.risks.splitlines() if CVE not in l)})
    _, doc, writer, _ = await run_with_writer("critical_cve", [as_answer(no_cve), as_answer(good)])
    assert doc.status == "published" and doc.writer_calls == 2
    assert doc.first_report is not None and ("missing_risk", "risks") in doc.first_report.kinds()
    assert "gh:dependabot:34" in writer.calls[1][-1].content  # the revision brief names what is missing


async def test_an_injected_fake_citation_is_caught_and_revised(mock_env):
    good = await oracle_draft("clean")
    fake = good.model_copy(update={"changes": good.changes + "\nSettlement is now twice as fast [jira:PAY-999]."})
    # The revision also rewrites the summary, which passed: only the failed section may be taken from it.
    revised = good.model_copy(update={"summary": "A different summary."})
    _, doc, writer, _ = await run_with_writer("clean", [as_answer(fake), as_answer(revised)])
    assert doc.first_report is not None
    assert [(i.kind, i.section, i.evidence_ids) for i in doc.first_report.issues] == [("unknown_citation", "changes", ("jira:PAY-999",))]
    assert doc.status == "published" and doc.report is not None and doc.report.ok
    revision = writer.calls[1][-1]
    assert isinstance(revision, HumanMessage) and "jira:PAY-999" in revision.content and "## changes" in revision.content
    assert "A different summary." not in doc.record.markdown and good.summary in doc.record.markdown  # type: ignore[union-attr]


async def test_a_document_still_invalid_after_the_revision_is_stored_and_not_published(mock_env):
    good = await oracle_draft("clean")
    fake = good.model_copy(update={"changes": good.changes + "\nSettlement is faster [jira:PAY-999]."})
    _, doc, _, releases = await run_with_writer("clean", [as_answer(fake), as_answer(fake)])
    assert doc.status == "rejected" and doc.record is not None and doc.record.status == "rejected"
    assert doc.record.issues and doc.release is None
    assert not any(r.tag_name == "v2.4.0" for r in releases)


async def test_an_invalid_writer_answer_twice_fails_without_storing(mock_env):
    store = InMemoryDocumentStore()
    _, doc, writer, _ = await run_with_writer("clean", ["no tool call", as_answer({"summary": "missing sections"})], store=store)
    assert doc.status == "failed" and doc.record is None and doc.writer_calls == 2 == len(writer.calls)
    assert [g.error_type for g in doc.gaps] == ["InvalidOutput", "InvalidOutput"]
    assert await store.versions(IDENTITY.correlation_id) == []


async def test_github_down_leaves_a_stored_unpublished_document_with_a_gap(mock_env):
    from release_copilot.agents.supervisor import build_supervisor, run_supervisor
    from tests.test_evidence import _github

    reference = await collect(Mocks())
    scripted = Scripted(scripts_for(reference))
    scripted.by_name["writer"] = FakeChatModel(responses=[as_answer(await oracle_draft("clean"))])
    mocks, store = Mocks(), InMemoryDocumentStore()
    mocks.fault(_github("releases"), status=503)
    async with toolbox(mocks) as box:
        result = await run_supervisor(build_supervisor(box, scripted.models, documents=store), IDENTITY, run_id=RUN_ID)
    doc = result.document
    assert doc is not None and doc.status == "unpublished" and doc.record is not None and doc.record.status == "valid"
    assert [(g.kind, g.retryable) for g in doc.gaps] == [("unavailable", True)]
    assert (await store.latest(IDENTITY.correlation_id)) == doc.record


async def test_a_rerun_on_unchanged_evidence_keeps_one_version(mock_env):
    store = InMemoryDocumentStore()
    good = await oracle_draft("clean")
    _, first, _, _ = await run_with_writer("clean", [as_answer(good)], store=store)
    _, second, _, _ = await run_with_writer("clean", [as_answer(good)], store=store)
    assert first.record is not None and second.record is not None
    assert first.record.version == second.record.version == 1
    assert isinstance(second, DocumentResult) and second.status == "published"



@pytest.mark.live
async def test_live_writer_produces_a_valid_document(mock_env):
    """A real writer model on critical_cve (scripted workers): the document validates within one revision. ``--live``."""
    from release_copilot.llm.factory import get_chat_model
    from tests.conftest import _clear_caches
    from tests.test_supervisor import REAL_LLM_ENV

    if REAL_LLM_ENV.get("RELEASE_COPILOT_LLM__PROVIDER", "openai") == "fake" or not any(k.endswith("_API_KEY") for k in REAL_LLM_ENV):
        pytest.skip("no real LLM configured (RELEASE_COPILOT_LLM__* in the environment or .env)")
    for key, value in REAL_LLM_ENV.items():
        mock_env.setenv(key, value)
    _clear_caches()
    _, doc, _, _ = await run_with_writer("critical_cve", [], writer=get_chat_model("writer"))
    print(doc.first_report, doc.report, doc.record and doc.record.markdown)
    assert doc.status == "published", doc.report
