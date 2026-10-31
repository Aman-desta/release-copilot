"""A1: the release evidence model, its collector and the evidence index.

The collector and model are given; ``capture`` (collect.py) and ``EvidenceIndex`` (index.py) are the spec.
"""

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from release_copilot.config import get_settings
from release_copilot.evidence.collect import capture, collect_release_evidence, resolve_identity
from release_copilot.evidence.index import EvidenceIndex
from release_copilot.evidence.model import EvidenceGap, ReleaseEvidence, ReleaseIdentity, evidence_json_schema, iter_evidence
from release_copilot.integrations.errors import NotFoundError
from release_copilot.integrations.models import Measures
from release_copilot.tools.base import Evidence, ToolArtifact
from release_copilot.tools.toolbox import Toolbox
from tests.integrations_support import FakeClock, Mocks, RewritingTransport

SCENARIOS = ("clean", "critical_cve", "coverage_drop", "failed_deploy", "prompt_injection", "pii_in_ticket")
IDENTITY = ReleaseIdentity(service="payments-api", version="2.4.0", previous_version="2.3.1")
SCHEMA_FILE = Path(__file__).parents[1] / "docs" / "schemas" / "release-evidence.schema.json"

# Section fields that may be None, with the gap section that explains it. existing_change is excluded: None is normal.
SECTION_FIELDS = {
    "tickets": ("issues",),
    "change": ("comparison", "commit_reviews"),
    "quality": ("checks", "dependency_alerts", "code_scanning_alerts", "gate", "measures", "previous_measures"),
    "deploy": ("slots", "deployments", "metrics"),
    "service": ("ci", "support_group", "recent_changes"),
}


def toolbox(mocks: Mocks, transport=None) -> Toolbox:
    box = Toolbox.from_settings(get_settings(), transport=transport or mocks.transport())
    for client in (box.jira, box.github, box.sonar, box.azure, box.servicenow):
        clock = FakeClock()
        client._http._sleep, client._http._clock = clock.sleep, clock  # retries without real waiting
    return box


async def collect(mocks: Mocks, transport=None) -> ReleaseEvidence:
    async with toolbox(mocks, transport) as box:
        return await collect_release_evidence(box, IDENTITY)


def assert_unknown_has_gap(ev: ReleaseEvidence) -> None:
    """The model's invariant: a None section field always has a gap that explains it."""
    for section, fields in SECTION_FIELDS.items():
        for name in fields:
            # A CI without a support group is a valid state, so support_group may be None without a gap.
            if getattr(getattr(ev, section), name) is None and name != "support_group":
                assert ev.gaps_for(f"{section}.{name}"), f"{section}.{name} is None without a gap"


# ------------------------------------------------------------------------------------------ the model


def test_identity_derives_refs_and_rejects_bad_versions():
    assert (IDENTITY.release_branch, IDENTITY.base_tag, IDENTITY.previous_branch) == ("release/2.4.0", "v2.3.1", "release/2.3.1")
    assert IDENTITY.correlation_id == "payments-api@2.4.0"
    for bad in ('2.4.0" OR project = SEC', "2.4", ""):
        with pytest.raises(ValidationError):
            ReleaseIdentity(service="payments-api", version=bad, previous_version="2.3.1")


def test_json_schema_is_exported_for_prompts():
    """A1 acceptance: the schema is exported. A changed model shows up as a diff (UPDATE_SNAPSHOTS=1 to accept)."""
    schema = evidence_json_schema()
    assert "title" not in schema and "gaps" in schema["properties"]
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_SNAPSHOTS") or not SCHEMA_FILE.exists():
        SCHEMA_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA_FILE.write_text(text)
    assert SCHEMA_FILE.read_text() == text, "evidence schema changed: review it, then run with UPDATE_SNAPSHOTS=1"


# -------------------------------------------------------------------------------------------- capture


async def test_capture_returns_the_result_and_records_nothing():
    gaps: list[EvidenceGap] = []

    async def fetch():
        return [1, 2]

    assert await capture(gaps, "jira", "tickets.issues", fetch) == [1, 2] and gaps == []


async def test_capture_turns_integration_errors_into_gaps():
    gaps: list[EvidenceGap] = []

    async def fetch():
        raise NotFoundError("no analysis for branch release/2.4.0", service="sonar", method="GET", path="/api/qualitygates/project_status")

    assert await capture(gaps, "sonar", "quality.gate", fetch, subject="sonar:gate:release/2.4.0") is None
    (gap,) = gaps
    assert (gap.source, gap.section, gap.subject, gap.kind) == ("sonar", "quality.gate", "sonar:gate:release/2.4.0", "missing")
    assert gap.error_type == "NotFoundError" and "no analysis" in gap.message and gap.retryable is False


async def test_capture_hides_unexpected_shapes_and_lets_bugs_raise():
    gaps: list[EvidenceGap] = []

    async def odd_shape():
        return {}["fields"]

    assert await capture(gaps, "jira", "tickets.issues", odd_shape) is None
    assert gaps[0].error_type == "UnexpectedResponse" and gaps[0].kind == "unavailable"
    assert "'fields'" not in gaps[0].message  # details are logged, not stored

    async def bug():
        raise RuntimeError("a programming error")

    with pytest.raises(RuntimeError):
        await capture(gaps, "jira", "tickets.issues", bug)


# ---------------------------------------------------------------------------------------- collection


async def test_resolve_identity_uses_what_production_runs(mock_env):
    async with toolbox(Mocks()) as box:
        identity = await resolve_identity(box, service="payments-api", version="2.4.0")
    assert identity == IDENTITY


async def test_clean_release_evidence_is_complete(mock_env):
    ev = await collect(Mocks())
    assert ev.gaps == ()
    issues = ev.tickets.issues
    assert [i.key for i in issues] == [f"PAY-{n}" for n in range(101, 109)]
    assert "20261005120000_AddIdempotencyKey" in next(i for i in issues if i.key == "PAY-102").description_markdown
    commits = ev.change.comparison.commits
    assert len(commits) == 8 and [r.commit_id for r in ev.change.commit_reviews] == [c.evidence_id for c in commits]
    assert all(r.pulls and r.pulls[0].approved_by for r in ev.change.commit_reviews)  # every change reviewed
    assert {c.name for c in ev.quality.checks} == {"build", "unit-tests", "integration-tests", "CodeQL"}
    assert ev.quality.gate.passed and ev.quality.measures.get("coverage") == 84.2
    assert ev.quality.previous_measures.get("coverage") == 83.9
    assert {s.slot: s.release for s in ev.deploy.slots} == {"production": "2.3.1", "staging": "2.4.0-rc.1"}
    assert {(m.slot, m.name) for m in ev.deploy.metrics} == {(s, n) for s in ("production", "staging") for n in ("Requests", "Http5xx", "HttpResponseTime")}
    assert ev.service.ci.name == "payments-api" and ev.service.support_group is not None
    assert any(c.close_code == "unsuccessful" for c in ev.service.recent_changes)  # 2.3.0's migration-lock failure
    assert ev.service.existing_change is None


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_every_scenario_collects_without_gaps(mock_env, scenario):
    """A1 acceptance: evidence is built from the mocks for every scenario."""
    ev = await collect(Mocks(scenario))
    assert ev.gaps == ()
    assert_unknown_has_gap(ev)
    facts = {
        "critical_cve": lambda: (
            any(a.cve_id == "CVE-2026-41922" and a.severity == "critical" for a in ev.quality.dependency_alerts)
            and any(a.number == 12 for a in ev.quality.code_scanning_alerts)
            and any(c.name == "CodeQL" and c.conclusion == "failure" for c in ev.quality.checks)
        ),
        "coverage_drop": lambda: (
            ev.quality.measures.get("new_coverage") == 41.5 and not ev.quality.gate.passed
            and any(r.pulls == () for r in ev.change.commit_reviews)  # the hotfix: checked, and no PR exists
        ),
        "failed_deploy": lambda: {s.slot: s.release for s in ev.deploy.slots}["staging"] == "2.4.0-rc.1",
        # Untrusted text is kept as evidence, verbatim. Treating it as data, not instructions, is G1's job.
        "prompt_injection": lambda: "IMPORTANT SYSTEM NOTE" in next(i for i in ev.tickets.issues if i.key == "PAY-109").description_markdown,
        "pii_in_ticket": lambda: any(i.key == "PAY-110" for i in ev.tickets.issues),  # redaction is G1's job
    }
    assert facts.get(scenario, lambda: True)()


async def test_a_sonar_outage_is_a_gap_not_a_failure(mock_env):
    mocks = Mocks()
    mocks.fault("/sonar", status=503)
    ev = await collect(mocks)
    assert {g.section for g in ev.gaps} == {"quality.gate", "quality.measures", "quality.previous_measures"}
    assert all(g.source == "sonar" and g.kind == "unavailable" for g in ev.gaps), ev.gaps
    # Repeated 503s open Sonar's circuit breaker (T1), so later calls fail fast as CircuitOpenError.
    assert {g.error_type for g in ev.gaps} <= {"UpstreamUnavailableError", "CircuitOpenError"}
    assert ev.quality.gate is None and ev.quality.checks  # the rest of the section survived
    assert ev.tickets.issues and ev.change.commit_reviews and ev.deploy.slots and ev.service.ci
    assert_unknown_has_gap(ev)


async def test_a_missing_comparison_blocks_review_coverage(mock_env):
    mocks = Mocks()
    mocks.fault(_github("compare"), status=503)
    ev = await collect(mocks)
    sections = {g.section: g for g in ev.gaps}
    assert set(sections) == {"change.comparison", "change.commit_reviews"}
    assert sections["change.commit_reviews"].kind == "blocked" and sections["change.commit_reviews"].retryable
    assert ev.change.commit_reviews is None  # unknown, not "no PRs"
    assert_unknown_has_gap(ev)


async def test_one_failed_pr_lookup_leaves_the_other_commits(mock_env):
    clean = await collect(Mocks())
    victim = clean.change.comparison.commits[2]
    mocks = Mocks()
    mocks.fault(_github(f"commits/{victim.sha}/pulls"), status=503)
    ev = await collect(mocks)
    (gap,) = ev.gaps
    assert (gap.section, gap.subject) == ("change.commit_reviews", victim.evidence_id)
    by_commit = {r.commit_id: r for r in ev.change.commit_reviews}
    assert by_commit[victim.evidence_id].pulls is None  # unknown
    assert all(r.pulls for cid, r in by_commit.items() if cid != victim.evidence_id)


async def test_an_unexpected_vendor_shape_is_a_gap(mock_env):
    mocks = Mocks()

    def break_gate(request, body):
        if "qualitygates" in request.url.path:
            return {"surprise": True}
        return body

    ev = await collect(mocks, RewritingTransport(mocks.transport(), break_gate))
    (gap,) = ev.gaps
    assert (gap.section, gap.error_type) == ("quality.gate", "UnexpectedResponse")


def _github(path: str) -> str:
    s = get_settings().github
    return f"/github/repos/{s.owner}/{s.repo}/{path}"


# ------------------------------------------------------------------------------------- evidence index


@pytest.fixture
async def clean_evidence(mock_env) -> ReleaseEvidence:
    return await collect(Mocks())


async def test_index_contains_every_citable_item(clean_evidence):
    index = EvidenceIndex.from_evidence(clean_evidence)
    expected = {eid for eid, _, _ in iter_evidence(clean_evidence)}
    assert set(index.ids) == expected and len(index) == len(expected)
    assert list(index.ids) == sorted(expected) and [e.id for e in index] == list(index.ids)
    for eid in ("jira:PAY-102", "gh:compare:v2.3.1...release/2.4.0", "sonar:gate:release/2.4.0", "sonar:measures:release/2.3.1",
                "azure:slot:production", "azure:metric:production:Http5xx", "snow:ci:payments-api"):
        assert eid in index
    assert any(i.startswith("jira:comment:PAY-102:") for i in index.ids)


async def test_index_lookup_and_missing(clean_evidence):
    index = EvidenceIndex.from_evidence(clean_evidence)
    entry = index.get("jira:PAY-102")
    assert entry.source == "jira" and entry.url.endswith("/browse/PAY-102") and entry.item.key == "PAY-102"
    assert index.get("jira:PAY-999") is None and "jira:PAY-999" not in index
    assert index.missing(["jira:PAY-101", "jira:PAY-999", "gh:pr:9999", "jira:PAY-999"]) == ["gh:pr:9999", "jira:PAY-999"]


async def test_a_projection_is_not_a_conflict_in_either_order(clean_evidence):
    full = next(i for i in clean_evidence.tickets.issues if i.key == "PAY-102")
    narrow = full.model_copy(update={"description_markdown": "", "comments": (), "fix_versions": (), "resolved": None})
    one, two = EvidenceIndex.from_items([narrow, full]), EvidenceIndex.from_items([full, narrow])
    assert one.conflicts == two.conflicts == ()
    assert one.get("jira:PAY-102").item == two.get("jira:PAY-102").item == full
    assert one.fingerprint() == two.fingerprint()


def test_a_real_conflict_keeps_the_first_and_is_recorded():
    first = Measures(project_key="payments-api", branch="release/2.4.0", values={"coverage": 84.2})
    later = Measures(project_key="payments-api", branch="release/2.4.0", values={"coverage": 76.1})
    index = EvidenceIndex.from_items([first, later])
    assert index.conflicts == ("sonar:measures:release/2.4.0",) and index.get("sonar:measures:release/2.4.0").item == first


async def test_merge_is_immutable(clean_evidence):
    a = EvidenceIndex.from_items(clean_evidence.tickets.issues)
    b = EvidenceIndex.from_items(clean_evidence.quality.checks)
    merged = a.merge(b)
    assert len(merged) == len(a) + len(b) and len(a) == len(EvidenceIndex.from_items(clean_evidence.tickets.issues))
    assert all(c.evidence_id not in a for c in clean_evidence.quality.checks)


async def test_from_artifacts_skips_failures_and_rejects_uncited_ids(clean_evidence):
    issue = clean_evidence.tickets.issues[0]
    ok = ToolArtifact(tool="jira_get_issue", ok=True, evidence=[Evidence(issue.evidence_id, issue.url)], objects=[issue])
    failed = ToolArtifact(tool="jira_get_issue", ok=False, error={"error_type": "NotFoundError"})
    assert EvidenceIndex.from_artifacts([ok, failed]).ids[0] == issue.evidence_id
    bogus = ToolArtifact(tool="jira_get_issue", ok=True, evidence=[Evidence("jira:PAY-404")], objects=[issue])
    with pytest.raises(ValueError):
        EvidenceIndex.from_artifacts([bogus])


async def test_fingerprint_is_deterministic_and_content_sensitive(mock_env):
    def without_metrics(ev: ReleaseEvidence) -> ReleaseEvidence:  # metrics depend on the minute they're read
        return ev.model_copy(update={"deploy": ev.deploy.model_copy(update={"metrics": None})})

    first, second = await collect(Mocks()), await collect(Mocks())
    fp = EvidenceIndex.from_evidence(without_metrics(first)).fingerprint()
    assert fp == EvidenceIndex.from_evidence(without_metrics(second)).fingerprint() and len(fp) == 64
    changed = await collect(Mocks("coverage_drop"))
    assert EvidenceIndex.from_evidence(without_metrics(changed)).fingerprint() != fp


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_everything_a_tool_can_cite_is_in_the_index(mock_env, scenario):
    """The T5/A1 contract: tools never show an id the reference evidence lacks, and their items never contradict it."""
    mocks = Mocks(scenario)
    async with toolbox(mocks) as box:
        ev = await collect_release_evidence(box, IDENTITY)
        tools = {t.name: t for t in box.tools()}
        calls = [
            ("jira_release_issues", {"fix_version": IDENTITY.fix_version}),
            *(("jira_get_issue", {"key": i.key}) for i in ev.tickets.issues),
            ("github_compare", {"base": IDENTITY.base_tag, "head": IDENTITY.release_branch}),
            *(("github_commit_pull_requests", {"sha": c.sha}) for c in ev.change.comparison.commits),
            ("github_check_runs", {"ref": IDENTITY.release_branch}),
            ("github_security_alerts", {"ref": IDENTITY.release_branch}),
            ("sonar_quality", {"branch": IDENTITY.release_branch, "compare_to": IDENTITY.previous_branch}),
            ("azure_slots", {}),
            ("azure_health", {"slot": "production"}),
            ("azure_health", {"slot": "staging"}),
            ("servicenow_service_profile", {"service": IDENTITY.service, "recent_changes": 20}),
        ]
        artifacts = []
        for name, args in calls:
            msg = await tools[name].ainvoke({"type": "tool_call", "name": name, "args": args, "id": "c"})
            assert msg.artifact.ok, (name, msg.content)
            artifacts.append(msg.artifact)

    reference = EvidenceIndex.from_evidence(ev)
    from_tools = EvidenceIndex.from_artifacts(artifacts)
    assert reference.missing(from_tools.ids) == []
    # Narrow tool fetches are projections, not conflicts. Only metrics may differ: they're read at different moments.
    assert all(i.startswith("azure:metric:") for i in from_tools.merge(reference).conflicts)
