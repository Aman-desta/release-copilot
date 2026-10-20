import pytest

from release_copilot.mocks.scenarios import SCENARIOS
from tests.mocks.conftest import GITHUB, JIRA, SONAR, scenario

REPO = "/github/repos/acme-fin/payments-api"
FIX_VERSION_JQL = 'project = PAY AND fixVersion = "2.4.0"'


def _gate(client, name):
    return client.get(
        "/sonar/api/qualitygates/project_status",
        params={"projectKey": "payments-api", "branch": "release/2.4.0"},
        headers={**SONAR, **scenario(name)},
    ).json()["projectStatus"]


def test_all_scenarios_build_and_are_listed(client):
    listed = client.get("/_admin/scenarios").json()
    assert {s["name"] for s in listed["scenarios"]} == set(SCENARIOS)
    for name in SCENARIOS:
        assert client.get(f"{REPO}/compare/v2.3.1...release/2.4.0", headers={**GITHUB, **scenario(name)}).status_code == 200


def test_unknown_scenario_is_rejected(client):
    assert client.get(REPO, headers={**GITHUB, **scenario("nope")}).status_code == 400


def test_critical_cve(client):
    h = {**GITHUB, **scenario("critical_cve")}
    critical = client.get(f"{REPO}/dependabot/alerts", params={"severity": "critical"}, headers=h).json()
    assert critical[0]["security_advisory"]["cve_id"] == "CVE-2026-41922"
    assert client.get(f"{REPO}/code-scanning/alerts", params={"ref": "release/2.4.0"}, headers=h).json()[0]["rule"]["security_severity_level"] == "high"
    checks = client.get(f"{REPO}/commits/v2.4.0-rc.1/check-runs", headers=h).json()["check_runs"]
    assert {r["name"]: r["conclusion"] for r in checks}["CodeQL"] == "failure"
    assert _gate(client, "critical_cve")["status"] == "ERROR"


def test_coverage_drop_has_untracked_commit_without_pr(client):
    assert _gate(client, "coverage_drop")["status"] == "ERROR"
    h = {**GITHUB, **scenario("coverage_drop")}
    commits = client.get(f"{REPO}/compare/v2.3.1...release/2.4.0", headers=h).json()["commits"]
    hotfix = commits[-1]
    assert hotfix["commit"]["message"].startswith("Hotfix") and client.get(f"{REPO}/commits/{hotfix['sha']}/pulls", headers=h).json() == []


@pytest.mark.parametrize("name,key,needle", [("prompt_injection", "PAY-109", "hooks.example.net"), ("pii_in_ticket", "PAY-110", "078-05-1120")])
def test_adversarial_tickets_are_in_the_fix_version(client, name, key, needle):
    issues = client.get("/jira/rest/api/3/search/jql", params={"jql": FIX_VERSION_JQL}, headers={**JIRA, **scenario(name)}).json()["issues"]
    issue = next(i for i in issues if i["key"] == key)
    assert needle in str(issue["fields"]["description"])
    # ...and absent from the clean world.
    clean = client.get("/jira/rest/api/3/search/jql", params={"jql": FIX_VERSION_JQL}, headers=JIRA).json()["issues"]
    assert key not in {i["key"] for i in clean}


def test_scenarios_have_isolated_mutable_state(client):
    adf = {"type": "doc", "version": 1, "content": []}
    client.post("/jira/rest/api/3/issue/PAY-101/comment", json={"body": adf}, headers={**JIRA, **scenario("critical_cve")})
    count = lambda name: client.get("/jira/rest/api/3/issue/PAY-101/comment", headers={**JIRA, **scenario(name)}).json()["total"]  # noqa: E731
    assert count("critical_cve") == 1 and count("clean") == 0
    client.post("/_admin/reset", params={"scenario": "critical_cve"})
    assert count("critical_cve") == 0


def test_active_scenario_switch(client):
    client.put("/_admin/scenario", json={"name": "coverage_drop"})
    assert client.get("/sonar/api/qualitygates/project_status", params={"projectKey": "payments-api"}, headers=SONAR).json()["projectStatus"]["status"] == "ERROR"


def test_counted_fault_then_recovery_with_retry_after(client):
    client.post("/_admin/faults", json={"path_prefix": "/jira", "status": 429, "times": 2, "retry_after": 3})
    statuses = [client.get("/jira/rest/api/3/myself", headers=JIRA) for _ in range(3)]
    assert [r.status_code for r in statuses] == [429, 429, 200]
    assert statuses[0].headers["retry-after"] == "3"
    log = client.get("/_admin/requests", params={"path_prefix": "/jira"}).json()
    assert [e["fault"] for e in log] == ["status", "status", None]


def test_scenario_scoped_fault_and_header_fault(client):
    client.post("/_admin/faults", json={"path_prefix": "/github", "status": 502, "scenario": "failed_deploy"})
    assert client.get(REPO, headers=GITHUB).status_code == 200
    assert client.get(REPO, headers={**GITHUB, **scenario("failed_deploy")}).status_code == 502
    assert client.get(REPO, headers={**GITHUB, "X-Mock-Fault": "503"}).status_code == 503


def test_latency_fault(client):
    client.post("/_admin/faults", json={"path_prefix": "/sonar", "kind": "latency", "delay_ms": 150, "times": 1})
    client.get("/sonar/api/authentication/validate", headers=SONAR)
    assert client.get("/_admin/requests", params={"path_prefix": "/sonar"}).json()[0]["ms"] >= 150
