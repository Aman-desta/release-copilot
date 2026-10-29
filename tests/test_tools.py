"""T5: the agent tool layer. Framework and non-Jira tools are given; ``jira_*`` and ``github_compare`` tests are the spec."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from release_copilot.config import get_settings
from release_copilot.tools import azure, github, servicenow, sonar
from release_copilot.tools.base import ToolArgs, ToolPayload, View, _dump, count_tokens, fit_to_budget, make_tool
from release_copilot.tools.toolbox import Toolbox
from tests.integrations_support import Mocks, RewritingTransport

SNAPSHOTS = Path(__file__).parent / "snapshots" / "tools"
ID_KEYS = ("id", "gate_id", "measures_id", "metric_ids")


@pytest.fixture
async def box(mock_env):
    async with Toolbox.from_settings(get_settings(), transport=Mocks().transport()) as b:
        yield b


def toolbox(mocks: Mocks) -> Toolbox:
    return Toolbox.from_settings(get_settings(), transport=mocks.transport())


def by_name(tools, name):
    return next(t for t in tools if t.name == name)


async def call(tool, **args) -> ToolMessage:
    """Invoke like an agent does (a tool call), so we get the ToolMessage with content AND artifact."""
    return await tool.ainvoke({"type": "tool_call", "name": tool.name, "args": args, "id": "call-1"})


def content(msg: ToolMessage) -> dict[str, Any]:
    return json.loads(msg.content)


def ids_in(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ID_KEYS:
                found |= set(v) if isinstance(v, list) else {v}
            else:
                found |= ids_in(v)
    elif isinstance(node, list):
        for v in node:
            found |= ids_in(v)
    return found


def assert_citable(msg: ToolMessage) -> None:
    """Every id the model can see is backed by evidence in the artifact (so citations can be verified)."""
    shown = ids_in(content(msg)["data"])
    assert shown, "a tool result must carry evidence ids"
    assert shown <= {e.id for e in msg.artifact.evidence}


# --------------------------------------------------------------------------------------- framework


def test_fit_to_budget_keeps_items_before_long_text():
    data = {"issues": [{"key": f"PAY-{i}", "summary": "x" * 200} for i in range(40)], "note": "y" * 3000}
    out, truncated = fit_to_budget(data, 1500)
    assert truncated and count_tokens(_dump(out)) <= 1500
    assert out["note"].endswith("…[truncated]") and len(out["issues"]) == 40  # long text went first


def test_fit_to_budget_marks_omitted_items():
    out, truncated = fit_to_budget({"items": [{"n": i, "t": "z" * 30} for i in range(200)]}, 300)
    assert truncated and out["items"][-1]["omitted"] + len(out["items"]) - 1 == 200


def test_write_tools_must_take_an_idempotency_key():
    class Args(ToolArgs):
        text: str

    async def fn(args):
        return ToolPayload(view=View())

    with pytest.raises(ValueError, match="idempotency_key"):
        make_tool(fn, name="post", description="d", args_schema=Args, access="write", domain="jira")


async def test_invalid_arguments_come_back_as_a_structured_error(box):
    tool = by_name(github.build(box), "github_commit_pull_requests")
    msg = await call(tool, sha="not-a-sha!")
    err = content(msg)["error"]
    assert err["error_type"] == "InvalidArguments" and err["details"][0]["field"] == "sha"


async def test_unknown_arguments_are_rejected(box):
    msg = await call(by_name(sonar.build(box), "sonar_quality"), branch="release/2.4.0", project="other")
    assert content(msg)["error"]["error_type"] == "InvalidArguments"


async def test_integration_errors_come_back_as_structured_results(box):
    msg = await call(by_name(github.build(box), "github_commit_pull_requests"), sha="deadbeefdeadbeef")
    assert content(msg) == {"ok": False, "error": {"error_type": "NotFoundError", "message": content(msg)["error"]["message"], "retryable": False, "service": "github"}}
    assert msg.artifact.ok is False


async def test_unexpected_vendor_shapes_are_structured_not_crashes(mock_env):
    def break_sonar(request, body):
        return {"surprise": True} if "/sonar/" in request.url.path else body

    mocks = Mocks()
    async with Toolbox.from_settings(get_settings(), transport=RewritingTransport(mocks.transport(), break_sonar)) as b:
        msg = await call(by_name(sonar.build(b), "sonar_quality"), branch="release/2.4.0")
    err = content(msg)["error"]
    assert err["error_type"] == "UnexpectedResponse" and "surprise" not in msg.content  # no raw detail leaks to the model


async def test_tool_output_respects_its_budget(mock_env):
    async with toolbox(Mocks("critical_cve")) as b:
        def alerts_tool(budget):
            return make_tool(lambda a: github._alerts(b, a), name="alerts", description="d",
                             args_schema=github.AlertsArgs, access="read", domain="github", budget=budget)

        full = await call(alerts_tool(5000), ref="release/2.4.0")
        budget = int(count_tokens(full.content) * 0.85)  # forces cutting, but stays above the essentials
        msg = await call(alerts_tool(budget), ref="release/2.4.0")
    assert "truncated" not in content(full)
    assert "truncated" in content(msg) and msg.artifact.truncated and count_tokens(msg.content) <= budget
    assert content(msg)["data"]["dependency_alerts"][0]["cve"] == "CVE-2026-41922"  # worst first survives


# ------------------------------------------------------------------------------------------ my tools


async def test_commit_pull_requests_shows_reviewers_and_unreviewed_pushes(mock_env):
    mocks = Mocks("coverage_drop")
    async with toolbox(mocks) as b:
        cmp = await b.github.compare("v2.3.1", "release/2.4.0")
        reviewed = await call(by_name(github.build(b), "github_commit_pull_requests"), sha=cmp.commits[0].sha)
        hotfix = await call(by_name(github.build(b), "github_commit_pull_requests"), sha=cmp.commits[-1].sha)
    pr = content(reviewed)["data"]["pull_requests"][0]
    assert pr["id"] == "gh:pr:411" and pr["approved_by"] == ["sosei"]
    assert content(hotfix)["data"]["pull_requests"] == [] and "pushed directly" in content(hotfix)["data"]["note"]
    assert_citable(reviewed)


async def test_check_runs_and_alerts_on_critical_cve(mock_env):
    async with toolbox(Mocks("critical_cve")) as b:
        checks = await call(by_name(github.build(b), "github_check_runs"), ref="release/2.4.0")
        alerts = await call(by_name(github.build(b), "github_security_alerts"), ref="release/2.4.0")
    assert content(checks)["data"]["all_passed"] is False and content(checks)["data"]["failing"] == ["CodeQL"]
    data = content(alerts)["data"]
    assert data["counts"] == {"critical": 1, "medium": 1, "high": 1}
    assert data["dependency_alerts"][0]["cve"] == "CVE-2026-41922"  # worst first
    assert data["code_scanning_alerts"][0]["location"].endswith("RefundRepository.cs:88")
    assert_citable(checks)
    assert_citable(alerts)


async def test_sonar_quality_with_comparison(mock_env):
    async with toolbox(Mocks("coverage_drop")) as b:
        msg = await call(by_name(sonar.build(b), "sonar_quality"), branch="release/2.4.0", compare_to="release/2.3.1")
    data = content(msg)["data"]
    assert data["gate"] == "ERROR" and data["failing_conditions"] == [{"metric": "new_coverage", "actual": "41.5", "rule": "< 80 fails"}]
    assert data["change_vs_previous"]["coverage"] == pytest.approx(76.1 - 83.9)
    assert_citable(msg)


async def test_azure_slots_and_health(mock_env):
    async with toolbox(Mocks()) as b:
        slots = await call(by_name(azure.build(b), "azure_slots"))
        health = await call(by_name(azure.build(b), "azure_health"), slot="production", minutes=60)
        bad = await call(by_name(azure.build(b), "azure_health"), slot="qa")
    by_slot = {s["slot"]: s for s in content(slots)["data"]["slots"]}
    assert by_slot["production"]["release"] == "2.3.1" and by_slot["production"]["active_deployment"]["id"] == "azure:deploy:production:dep-2.3.1"
    assert content(health)["data"]["error_rate_latest"] < 0.01
    assert content(bad)["error"]["error_type"] == "InvalidArguments"  # enum: only production | staging
    assert_citable(slots)
    assert_citable(health)


async def test_servicenow_profile_surfaces_past_failures(box):
    msg = await call(by_name(servicenow.build(box), "servicenow_service_profile"), service="payments-api")
    data = content(msg)["data"]
    assert data["business_criticality"] == "1 - most critical" and data["support_group"] == "Payments Engineering"
    failed = next(c for c in data["recent_changes"] if c["outcome"] == "unsuccessful")
    assert failed["id"] == "snow:change:CHG0030012" and "migration lock" in failed["notes"]
    missing = await call(by_name(servicenow.build(box), "servicenow_service_profile"), service="nope")
    assert content(missing)["data"] == {"service": "nope", "found": False, "recent_changes": []}
    assert_citable(msg)


# ------------------------------------------------------------------------- Toolbox + Jira (spec)


async def test_toolbox_is_read_only_by_default(box):
    read = box.tools()
    names = {t.name for t in read}
    assert {"jira_release_issues", "jira_get_issue", "github_compare", "sonar_quality", "azure_health"} <= names
    assert "jira_add_comment" not in names and all(t.metadata["access"] == "read" for t in read)
    writers = box.tools(domains={"jira"}, access={"read", "write"})
    assert "jira_add_comment" in {t.name for t in writers}
    with pytest.raises(ValueError, match="unknown"):
        box.tools(domains={"slack"})


async def test_jira_release_issues_is_compact_and_citable(box):
    """T5 acceptance: the 8-ticket fix version fits in 2k tokens."""
    msg = await call(by_name(box.tools(), "jira_release_issues"), fix_version="2.4.0")
    assert count_tokens(msg.content) <= 2000 and "truncated" not in content(msg)
    text = msg.content
    assert all(f"PAY-{n}" in text for n in range(101, 109))
    assert "Apple Pay payment tokens are decrypted" not in text  # descriptions belong to jira_get_issue
    assert_citable(msg)
    assert {e.id for e in msg.artifact.evidence} == {f"jira:PAY-{n}" for n in range(101, 109)}
    assert all(e.url and e.url.endswith(e.id.split(":")[1]) for e in msg.artifact.evidence)  # source links
    assert all(not issue.description_markdown for issue in msg.artifact.objects)


@pytest.mark.parametrize("fix_version", ['2.4.0" OR project = SEC', "2.4.0 ORDER BY", ""])
async def test_jira_release_issues_rejects_jql_injection(box, fix_version):
    msg = await call(by_name(box.tools(), "jira_release_issues"), fix_version=fix_version)
    assert content(msg)["error"]["error_type"] == "InvalidArguments"

async def test_jira_get_issue_has_description_and_comments(box):
    msg = await call(by_name(box.tools(), "jira_get_issue"), key="PAY-102")
    text = msg.content
    assert "20261005120000_AddIdempotencyKey" in text and "Confirmed with DBA" in text
    assert_citable(msg)


async def test_jira_get_issue_not_found_is_structured(box):
    """T5 acceptance: a 404 is a structured result, not an exception."""
    msg = await call(by_name(box.tools(), "jira_get_issue"), key="PAY-999")
    assert content(msg)["ok"] is False and content(msg)["error"]["error_type"] == "NotFoundError"
    bad = await call(by_name(box.tools(), "jira_get_issue"), key="PAY-1 OR 1=1")
    assert content(bad)["error"]["error_type"] == "InvalidArguments"


async def test_jira_add_comment_is_idempotent(mock_env):
    mocks = Mocks()
    idempotency_key = "run-42:pay-102:release-note"
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]
    async with toolbox(mocks) as b:
        post = by_name(b.tools(access={"write"}), "jira_add_comment")
        first = await call(post, key="PAY-102", text="Included in release 2.4.0.", idempotency_key=idempotency_key)
        comments = mocks.state.world()["jira"]["issues"]["PAY-102"]["fields"]["comment"]["comments"]
        assert len(comments) == 2  # the seeded comment + exactly one of ours
        assert content(first)["ok"]
        assert ids_in(content(first)["data"])

        comments[-1]["body"] = {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Keep before"}]},
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "— edited! REF "},
                        {"type": "text", "text": digest, "marks": [{"type": "strong"}]},
                        {"type": "text", "text": " (updated)"},
                    ],
                },
                {"type": "paragraph", "content": [{"type": "text", "text": "Keep after"}]},
            ],
        }
        again = await call(post, key="PAY-102", text="Included in release 2.4.0.", idempotency_key=idempotency_key)

    assert len(comments) == 2
    assert content(again)["ok"]
    assert ids_in(content(first)["data"]) == ids_in(content(again)["data"])  # the same comment both times
    result_body = content(again)["data"]["body_markdown"]
    assert "Keep before" in result_body and "Keep after" in result_body
    assert digest not in result_body and "edited!" not in result_body
    stored_body = json.dumps(comments[-1]["body"], ensure_ascii=False)
    assert "— edited! REF " in stored_body and digest in stored_body
    assert "<!-- release-copilot-idempotency:" not in stored_body


async def test_github_compare_is_compact_and_flags_risky_files(box):
    msg = await call(by_name(box.tools(), "github_compare"), base="v2.3.1", head="release/2.4.0")
    data, text = content(msg)["data"], msg.content
    assert count_tokens(text) <= 2000
    assert "PAY-101" in text and "PAY-108" in text  # ticket keys per commit
    assert "20261005120000_AddIdempotencyKey" in text  # migrations must stand out
    assert "appsettings.Production.json" in text  # so must production config
    assert_citable(msg)
    assert data is not None


async def test_github_compare_shows_untracked_commits(mock_env):
    async with toolbox(Mocks("coverage_drop")) as b:
        msg = await call(by_name(b.tools(), "github_compare"), base="v2.3.1", head="release/2.4.0")
    assert "Hotfix: raise capture timeout to 45s" in msg.content  # a commit with no ticket must be visible


# --------------------------------------------------------------------------------- schema snapshots


async def test_tool_schemas_match_snapshots(box):
    """What the model sees is an API: a change to a tool's name, description or arguments must be deliberate.

    Missing snapshots are written on first run. To accept a change: UPDATE_SNAPSHOTS=1 uv run pytest tests/test_tools.py
    """
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    update = os.environ.get("UPDATE_SNAPSHOTS") == "1"
    changed = []
    for tool in box.all_tools():
        schema = json.dumps(convert_to_openai_tool(tool), indent=2, sort_keys=True) + "\n"
        path = SNAPSHOTS / f"{tool.name}.json"
        if update or not path.exists():
            path.write_text(schema)
        elif path.read_text() != schema:
            changed.append(tool.name)
    assert not changed, f"tool schemas changed: {changed}; review, then run with UPDATE_SNAPSHOTS=1"
