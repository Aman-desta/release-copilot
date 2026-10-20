from tests.mocks.conftest import API_VERSION, GITHUB, JIRA, SITE_URL, SNOW, SONAR, scenario

REPO = "/github/repos/acme-fin/payments-api"


# ---------------------------------------------------------------------------------------------- auth


def test_every_service_rejects_missing_credentials(client):
    assert client.get("/jira/rest/api/3/myself").json()["errorMessages"]
    assert client.get(REPO).json() == {"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest"}
    assert client.get("/sonar/api/authentication/validate").status_code == 401
    assert client.get("/servicenow/api/now/table/change_request").json()["error"]["message"] == "User Not Authenticated"
    assert client.get(SITE_URL, params=API_VERSION).json()["error"]["code"] == "InvalidAuthenticationToken"


def test_azure_token_flow_validates_client_and_scope(client):
    from tests.mocks.conftest import S

    url = f"/azure/login/{S.azure_tenant_id}/oauth2/v2.0/token"
    bad = client.post(url, data={"grant_type": "client_credentials", "client_id": S.azure_client_id, "client_secret": "nope", "scope": "https://management.azure.com/.default"})
    assert bad.status_code == 401 and bad.json()["error"] == "invalid_client"
    wrong_scope = client.post(url, data={"grant_type": "client_credentials", "client_id": S.azure_client_id, "client_secret": S.azure_client_secret, "scope": "api://x"})
    assert wrong_scope.json()["error"] == "invalid_scope"


def test_azure_requires_api_version(client, azure):
    resp = client.get(SITE_URL, headers=azure)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "MissingApiVersionParameter"


# ---------------------------------------------------------------------------------------------- jira


def test_jira_search_by_fix_version_and_paging(client):
    jql = 'project = PAY AND fixVersion = "2.4.0" ORDER BY key ASC'
    first = client.get("/jira/rest/api/3/search/jql", params={"jql": jql, "maxResults": 5, "fields": "summary,status"}, headers=JIRA).json()
    assert [i["key"] for i in first["issues"]] == ["PAY-101", "PAY-102", "PAY-103", "PAY-104", "PAY-105"]
    assert set(first["issues"][0]["fields"]) == {"summary", "status"}
    assert first["isLast"] is False
    rest = client.get("/jira/rest/api/3/search/jql", params={"jql": jql, "maxResults": 5, "nextPageToken": first["nextPageToken"]}, headers=JIRA).json()
    assert [i["key"] for i in rest["issues"]] == ["PAY-106", "PAY-107", "PAY-108"] and rest["isLast"] is True


def test_jira_jql_operators_and_errors(client):
    def keys(jql):
        return [i["key"] for i in client.get("/jira/rest/api/3/search", params={"jql": jql}, headers=JIRA).json()["issues"]]

    assert keys("issuetype = Bug AND component in (refunds)") == ["PAY-103", "PAY-105"]
    assert keys('labels = db-migration') == ["PAY-102"]
    assert keys('text ~ "Service Bus"') == ["PAY-103"]
    assert keys("key not in (PAY-101, PAY-102) AND priority = High") == ["PAY-105"]
    assert client.get("/jira/rest/api/3/search", params={"jql": "status = Done OR status = x"}, headers=JIRA).status_code == 400
    assert client.get("/jira/rest/api/3/search", params={"jql": "bogus = 1"}, headers=JIRA).status_code == 400


def test_jira_description_is_adf_and_comments_require_adf(client):
    issue = client.get("/jira/rest/api/3/issue/PAY-102", headers=JIRA).json()
    assert issue["fields"]["description"]["type"] == "doc"
    assert client.post("/jira/rest/api/3/issue/PAY-102/comment", json={"body": "plain"}, headers=JIRA).status_code == 400
    adf = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Released"}]}]}
    assert client.post("/jira/rest/api/3/issue/PAY-102/comment", json={"body": adf}, headers=JIRA).status_code == 201
    assert client.get("/jira/rest/api/3/issue/PAY-102/comment", headers=JIRA).json()["total"] == 2


def test_jira_transition(client):
    assert client.post("/jira/rest/api/3/issue/PAY-101/transitions", json={"transition": {"id": "41"}}, headers=JIRA).status_code == 204
    assert client.get("/jira/rest/api/3/issue/PAY-101", headers=JIRA).json()["fields"]["status"]["name"] == "Released"


# -------------------------------------------------------------------------------------------- github


def test_github_compare_aggregates_commits_and_files(client):
    cmp = client.get(f"{REPO}/compare/v2.3.1...release/2.4.0", headers=GITHUB).json()
    assert cmp["total_commits"] == 8 and cmp["status"] == "ahead"
    messages = [c["commit"]["message"] for c in cmp["commits"]]
    assert messages[0].startswith("PAY-101") and messages[-1].startswith("PAY-108")
    migration = next(f for f in cmp["files"] if "Migrations" in f["filename"])
    assert migration["status"] == "added"


def test_github_pulls_paginate_with_link_header(client):
    resp = client.get(f"{REPO}/pulls", params={"state": "closed", "per_page": 3}, headers=GITHUB)
    assert len(resp.json()) == 3 and 'rel="next"' in resp.headers["link"]
    assert resp.json()[0]["merged"] is True


def test_github_commit_pulls_reviews_and_checks(client):
    head = client.get(f"{REPO}/commits/v2.4.0-rc.1", headers=GITHUB).json()
    assert head["stats"]["total"] > 0
    pulls = client.get(f"{REPO}/commits/{head['sha']}/pulls", headers=GITHUB).json()
    assert pulls[0]["number"] == 418
    assert client.get(f"{REPO}/pulls/418/reviews", headers=GITHUB).json()[0]["state"] == "APPROVED"
    checks = client.get(f"{REPO}/commits/{head['sha']}/check-runs", headers=GITHUB).json()
    assert {r["conclusion"] for r in checks["check_runs"]} == {"success"}


def test_github_alerts_filter(client):
    alerts = client.get(f"{REPO}/dependabot/alerts", params={"state": "open"}, headers=GITHUB).json()
    assert [a["security_advisory"]["severity"] for a in alerts] == ["medium"]
    assert client.get(f"{REPO}/dependabot/alerts", params={"severity": "critical,high"}, headers=GITHUB).json() == []
    assert client.get(f"{REPO}/code-scanning/alerts", headers=GITHUB).json() == []


def test_github_release_create_is_not_idempotent(client):
    body = {"tag_name": "v2.4.0", "name": "2.4.0", "body": "notes"}
    assert client.post(f"{REPO}/releases", json=body, headers=GITHUB).status_code == 201
    assert client.post(f"{REPO}/releases", json=body, headers=GITHUB).json()["errors"][0]["code"] == "already_exists"


# --------------------------------------------------------------------------------------------- sonar


def test_sonar_measures_and_gate(client):
    m = client.get("/sonar/api/measures/component", params={"component": "payments-api", "metricKeys": "coverage,new_coverage", "branch": "release/2.4.0"}, headers=SONAR).json()
    measures = {x["metric"]: x for x in m["component"]["measures"]}
    assert measures["coverage"]["value"] == "84.2" and measures["new_coverage"]["period"]["value"] == "88.0"
    gate = client.get("/sonar/api/qualitygates/project_status", params={"projectKey": "payments-api", "branch": "release/2.4.0"}, headers=SONAR).json()
    assert gate["projectStatus"]["status"] == "OK"


# ---------------------------------------------------------------------------------------- servicenow


def test_servicenow_query_and_references(client):
    rows = client.get(
        "/servicenow/api/now/table/change_request",
        params={"sysparm_query": "cmdb_ci=ci-payments-api^ORDERBYDESCsys_created_on", "sysparm_display_value": "all"},
        headers=SNOW,
    )
    result = rows.json()["result"]
    assert rows.headers["x-total-count"] == "2"
    assert result[0]["number"] == "CHG0030019" and result[1]["close_code"] == "unsuccessful"
    assert result[0]["assignment_group"]["display_value"] == "Payments Engineering"


def test_servicenow_change_lifecycle_enforces_approval(client):
    from tests.mocks.conftest import JIRA  # noqa: F401  (fixtures share one app per test)

    created = client.post(
        "/servicenow/api/now/table/change_request",
        json={"short_description": "Deploy payments-api 2.4.0", "cmdb_ci": "ci-payments-api", "assignment_group": "g-payments", "risk": "3", "correlation_id": "payments-api@2.4.0"},
        headers=SNOW,
    )
    assert created.status_code == 201
    change = created.json()["result"]
    assert change["number"] == "CHG0030027" and change["state"] == "-5" and change["cmdb_ci"]["value"] == "ci-payments-api"
    cr = f"/servicenow/api/now/table/change_request/{change['sys_id']}"

    # The integration user can neither self-approve nor schedule an unapproved change.
    assert client.patch(cr, json={"approval": "approved"}, headers=SNOW).status_code == 403
    assert client.patch(cr, json={"state": "-2"}, headers=SNOW).status_code == 403
    assert client.post("/servicenow/api/now/table/change_request", json={"short_description": "x", "risk": "9"}, headers=SNOW).status_code == 400

    approver = client.post("/servicenow/api/now/table/sysapproval_approver", json={"sysapproval": change["sys_id"], "approver": "u-marcus"}, headers=SNOW).json()["result"]
    assert client.get(cr, headers=SNOW).json()["result"]["state"] == "-3"

    decided = client.post(f"/_admin/servicenow/approvals/{approver['sys_id']}", json={"state": "approved", "comments": "LGTM"}).json()
    assert decided["change"]["approval"] == "approved" and decided["change"]["state"] == "-2"
    assert client.patch(cr, json={"state": "-1"}, headers=SNOW).status_code == 200


def test_servicenow_read_only_tables(client):
    assert client.post("/servicenow/api/now/table/sys_user", json={"name": "x"}, headers=SNOW).status_code == 403


# ----------------------------------------------------------------------------------------- azure


def test_azure_site_slots_and_deployments(client, azure):
    site = client.get(SITE_URL, params=API_VERSION, headers=azure).json()
    assert site["tags"]["release"] == "2.3.1" and site["properties"]["state"] == "Running"
    slots = client.get(f"{SITE_URL}/slots", params=API_VERSION, headers=azure).json()["value"]
    assert slots[0]["tags"]["release"] == "2.4.0-rc.1"
    deps = client.get(f"{SITE_URL}/deployments", params=API_VERSION, headers=azure).json()["value"]
    assert deps[0]["properties"]["active"] is True and deps[0]["properties"]["status"] == 4


def _latest(client, azure, metric, slot=None, hdrs=None):
    url = f"{SITE_URL}/slots/{slot}/providers/Microsoft.Insights/metrics" if slot else f"{SITE_URL}/providers/Microsoft.Insights/metrics"
    body = client.get(url, params={**API_VERSION, "metricnames": metric}, headers={**azure, **(hdrs or {})}).json()
    point = body["value"][0]["timeseries"][0]["data"][-1]
    return point.get("total", point.get("average"))


def test_azure_swap_is_async_and_moves_release(client, azure):
    resp = client.post(f"{SITE_URL}/slots/staging/slotsswap", params=API_VERSION, json={"targetSlot": "production"}, headers=azure)
    assert resp.status_code == 202
    op = client.get(resp.headers["azure-asyncoperation"].replace("http://testserver", ""), headers=azure)
    assert op.json()["status"] == "Succeeded"
    assert client.get(SITE_URL, params=API_VERSION, headers=azure).json()["tags"]["release"] == "2.4.0-rc.1"
    assert _latest(client, azure, "Http5xx") < 5  # clean scenario stays healthy


def test_failed_deploy_degrades_only_after_swap_in_production(client, azure):
    h = scenario("failed_deploy")
    assert _latest(client, azure, "Http5xx", slot="staging", hdrs=h) == 0
    assert _latest(client, azure, "Http5xx", hdrs=h) < 5
    client.post(f"{SITE_URL}/slots/staging/slotsswap", params=API_VERSION, json={"targetSlot": "production"}, headers={**azure, **h})
    assert _latest(client, azure, "Http5xx", hdrs=h) > 100
    assert _latest(client, azure, "HttpResponseTime", hdrs=h) > 1.5
    # Rollback = swap back.
    client.post(f"{SITE_URL}/slots/staging/slotsswap", params=API_VERSION, json={"targetSlot": "production"}, headers={**azure, **h})
    assert _latest(client, azure, "Http5xx", hdrs=h) < 5


def test_azure_unknown_metric(client, azure):
    resp = client.get(f"{SITE_URL}/providers/Microsoft.Insights/metrics", params={**API_VERSION, "metricnames": "Nope"}, headers=azure)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------------------- notify


def test_notifications_are_recorded(client):
    assert client.post("/notify/teams/release-approvals", json={"text": "CHG0030027 needs approval"}).text == "1"
    assert client.post("/notify/slack/payments", json={}).status_code == 400
    sent = client.get("/_admin/notifications").json()
    assert sent[0]["channel"] == "release-approvals" and sent[0]["scenario"] == "clean"
