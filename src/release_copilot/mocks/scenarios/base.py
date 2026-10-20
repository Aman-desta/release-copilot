"""The base "world": one release (payments-api 2.4.0) as seen by every enterprise system.

The data is stored in each real API's own shape, so routers only filter it and add links. Each scenario
deep-copies this world and edits it (see ``variants.py``).

Fictional names only: org ``acme-fin``; advisories and CVE ids are invented.
"""

import hashlib
from typing import Any

from release_copilot.mocks.adf import doc

ORG = "acme-fin"
REPO = "payments-api"
REPO_FULL = f"{ORG}/{REPO}"
JIRA_PROJECT = "PAY"
FIX_VERSION = "2.4.0"
PREV_VERSION = "2.3.1"
RELEASE_BRANCH = "release/2.4.0"

SUBSCRIPTION_ID = "6f1c2a9e-3b4d-4e5f-8a7b-0c1d2e3f4a5b"
RESOURCE_GROUP = "rg-payments-prod"
SITE = "app-payments-api-prod"
LOCATION = "East US 2"

USERS = {
    "dana": {"accountId": "712020:dana-reyes", "displayName": "Dana Reyes", "emailAddress": "dana.reyes@acme-fin.test"},
    "li": {"accountId": "712020:li-wei", "displayName": "Li Wei", "emailAddress": "li.wei@acme-fin.test"},
    "sam": {"accountId": "712020:sam-osei", "displayName": "Sam Osei", "emailAddress": "sam.osei@acme-fin.test"},
    "priya": {"accountId": "712020:priya-nair", "displayName": "Priya Nair", "emailAddress": "priya.nair@acme-fin.test"},
}
GH_LOGINS = {"dana": "dreyes", "li": "liwei-dev", "sam": "sosei", "priya": "pnair"}


def sha(seed: str) -> str:
    return hashlib.sha1(seed.encode()).hexdigest()


# --------------------------------------------------------------------------------------------- Jira


def jira_issue(
    num: int,
    summary: str,
    issuetype: str,
    description: list[str],
    *,
    assignee: str,
    priority: str = "Medium",
    status: str = "Done",
    labels: list[str] | None = None,
    components: list[str] | None = None,
    created: str = "2026-09-22T09:00:00.000+0000",
    updated: str = "2026-10-08T16:30:00.000+0000",
    comments: list[tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    key = f"{JIRA_PROJECT}-{num}"
    category = {"Done": "done", "In Progress": "indeterminate"}.get(status, "new")
    return {
        "id": str(10000 + num),
        "key": key,
        "fields": {
            "summary": summary,
            "issuetype": {"name": issuetype, "subtask": False},
            "status": {"name": status, "statusCategory": {"key": category}},
            "priority": {"name": priority},
            "assignee": USERS[assignee],
            "reporter": USERS["dana"],
            "labels": labels or [],
            "components": [{"name": c} for c in components or []],
            "fixVersions": [{"id": "10240", "name": FIX_VERSION, "released": False}],
            "project": {"key": JIRA_PROJECT, "name": "Payments API"},
            "description": doc(*description),
            "created": created,
            "updated": updated,
            "resolutiondate": updated if status == "Done" else None,
            "comment": {
                "comments": [
                    {
                        "id": str(20000 + num * 10 + i),
                        "author": USERS[who],
                        "body": doc(body),
                        "created": when,
                    }
                    for i, (who, body, when) in enumerate(comments or [])
                ],
            },
        },
    }


BASE_ISSUES = [
    jira_issue(
        101,
        "Add Apple Pay tokenization to checkout",
        "Story",
        [
            "As a merchant I want customers to pay with Apple Pay so that mobile checkout conversion improves.",
            "Acceptance criteria:",
            "- Apple Pay payment tokens are decrypted by ApplePayTokenizer and never logged\n"
            "- Checkout returns 422 for expired tokens\n"
            "- Feature is behind the flag checkout.applepay.enabled (default off)",
        ],
        assignee="li",
        priority="High",
        components=["checkout"],
        labels=["feature-flag"],
    ),
    jira_issue(
        102,
        "Idempotency keys for the payment capture endpoint",
        "Story",
        [
            "Clients retry POST /captures on network errors, which can double-capture. Accept an Idempotency-Key "
            "header and return the original response for repeats within 24h.",
            "Adds column IdempotencyKey (nullable, unique index) to the Captures table via EF Core migration "
            "20261005120000_AddIdempotencyKey. The migration is additive; rollback leaves the column in place.",
        ],
        assignee="sam",
        priority="High",
        components=["capture"],
        labels=["db-migration"],
        comments=[("dana", "Confirmed with DBA: index build is online, ~40s on prod size.", "2026-10-04T11:02:00.000+0000")],
    ),
    jira_issue(
        103,
        "Refund webhook retried twice on timeout",
        "Bug",
        [
            "When the merchant endpoint times out after 10s, RefundWebhookDispatcher retries AND the Service Bus "
            "message is redelivered, so merchants receive the refund.succeeded event twice.",
            "Fix: complete the message only after the dispatcher's own retry policy finishes.",
        ],
        assignee="sam",
        priority="Medium",
        components=["refunds"],
    ),
    jira_issue(
        104,
        "Upgrade Azure.Messaging.ServiceBus to 7.18.2",
        "Task",
        ["Routine dependency upgrade; picks up AMQP connection recovery fixes."],
        assignee="priya",
        priority="Low",
        labels=["dependencies"],
    ),
    jira_issue(
        105,
        "JPY refunds rounded to two decimals",
        "Bug",
        [
            "Refund amounts for zero-decimal currencies (JPY, KRW) are rounded to 2 decimals, producing "
            "refunds of 1000.00 JPY that the processor rejects.",
            "Fix: use ISO 4217 minor units in CurrencyRounding.",
        ],
        assignee="li",
        priority="High",
        components=["refunds"],
    ),
    jira_issue(
        106,
        "Emit p95 latency metric for the capture flow",
        "Story",
        ["Publish capture.duration histogram to Application Insights so the SLO dashboard can show p95."],
        assignee="priya",
        components=["observability"],
    ),
    jira_issue(
        107,
        "Move settlement batch job to Azure Functions",
        "Story",
        [
            "The nightly settlement batch runs as a hosted service inside the web app and competes for CPU. "
            "Move it to a timer-triggered Azure Function (Payments.Settlement.Functions).",
            "The web app's hosted service is disabled by the setting Settlement:RunInWebApp=false.",
        ],
        assignee="dana",
        components=["settlement"],
        labels=["infra"],
    ),
    jira_issue(
        108,
        "NullReferenceException when merchant has no default currency",
        "Bug",
        ["MerchantService.GetDefaultCurrency throws for merchants onboarded before 2024. Fall back to the account's country currency."],
        assignee="li",
        priority="Medium",
        components=["merchants"],
    ),
]

# ------------------------------------------------------------------------------------------- GitHub


def commit(seed: str, message: str, who: str, date: str, files: list[tuple[str, str, int, int]]) -> dict[str, Any]:
    return {
        "sha": sha(seed),
        "commit": {
            "message": message,
            "author": {"name": USERS[who]["displayName"], "email": USERS[who]["emailAddress"], "date": date},
        },
        "author": {"login": GH_LOGINS[who]},
        "files": [
            {"filename": f, "status": s, "additions": a, "deletions": d, "changes": a + d} for f, s, a, d in files
        ],
    }


API = "src/Payments.Api"
BASE_COMMITS = [
    commit("rel-2.3.1", "Release 2.3.1", "dana", "2026-09-18T15:00:00Z", [(f"{API}/Payments.Api.csproj", "modified", 1, 1)]),
    commit(
        "pay-101",
        "PAY-101: Add Apple Pay tokenization to checkout",
        "li",
        "2026-09-29T10:12:00Z",
        [
            (f"{API}/Controllers/CheckoutController.cs", "modified", 48, 6),
            (f"{API}/Services/ApplePayTokenizer.cs", "added", 132, 0),
            ("tests/Payments.Api.Tests/ApplePayTokenizerTests.cs", "added", 96, 0),
        ],
    ),
    commit(
        "pay-102",
        "PAY-102: Idempotency keys for capture endpoint",
        "sam",
        "2026-10-05T12:30:00Z",
        [
            (f"{API}/Controllers/CaptureController.cs", "modified", 37, 4),
            (f"{API}/Data/Migrations/20261005120000_AddIdempotencyKey.cs", "added", 41, 0),
            (f"{API}/Data/PaymentsDbContext.cs", "modified", 6, 0),
            ("tests/Payments.Api.Tests/CaptureIdempotencyTests.cs", "added", 118, 0),
        ],
    ),
    commit(
        "pay-103",
        "PAY-103: Complete Service Bus message after dispatcher retries finish",
        "sam",
        "2026-10-01T09:45:00Z",
        [(f"{API}/Services/RefundWebhookDispatcher.cs", "modified", 22, 15), ("tests/Payments.Api.Tests/RefundWebhookDispatcherTests.cs", "modified", 40, 3)],
    ),
    commit(
        "pay-104",
        "PAY-104: Bump Azure.Messaging.ServiceBus to 7.18.2",
        "priya",
        "2026-09-30T14:05:00Z",
        [(f"{API}/Payments.Api.csproj", "modified", 1, 1)],
    ),
    commit(
        "pay-105",
        "PAY-105: Use ISO 4217 minor units when rounding refunds",
        "li",
        "2026-10-02T16:20:00Z",
        [
            (f"{API}/Services/CurrencyRounding.cs", "modified", 19, 7),
            (f"{API}/Data/RefundRepository.cs", "modified", 8, 2),
            ("tests/Payments.Api.Tests/CurrencyRoundingTests.cs", "modified", 55, 0),
        ],
    ),
    commit(
        "pay-106",
        "PAY-106: Emit capture.duration histogram",
        "priya",
        "2026-10-03T11:00:00Z",
        [(f"{API}/Telemetry/CaptureMetrics.cs", "added", 44, 0), (f"{API}/Controllers/CaptureController.cs", "modified", 5, 0)],
    ),
    commit(
        "pay-107",
        "PAY-107: Move settlement batch to timer-triggered Azure Function",
        "dana",
        "2026-10-07T13:40:00Z",
        [
            ("src/Payments.Settlement.Functions/SettlementTimer.cs", "added", 88, 0),
            ("src/Payments.Settlement.Functions/host.json", "added", 12, 0),
            (f"{API}/HostedServices/SettlementBatchService.cs", "modified", 4, 1),
            (f"{API}/appsettings.Production.json", "modified", 3, 0),
        ],
    ),
    commit(
        "pay-108",
        "PAY-108: Fall back to account country currency",
        "li",
        "2026-10-08T10:15:00Z",
        [(f"{API}/Services/MerchantService.cs", "modified", 14, 2), ("tests/Payments.Api.Tests/MerchantServiceTests.cs", "modified", 30, 0)],
    ),
]


def pull(number: int, c: dict[str, Any], body: str, reviewer: str, merged_at: str) -> dict[str, Any]:
    title = c["commit"]["message"]
    author_login = c["author"]["login"]
    return {
        "number": number,
        "title": title,
        "state": "closed",
        "body": body,
        "user": {"login": author_login},
        "head": {"ref": f"feature/{title.split(':')[0].lower()}", "sha": c["sha"]},
        "base": {"ref": "main"},
        "merged_at": merged_at,
        "merge_commit_sha": c["sha"],
        "labels": [],
        "_reviews": [{"id": number * 100 + 1, "user": {"login": reviewer}, "state": "APPROVED", "submitted_at": merged_at}],
    }


def base_pulls(commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_msg = {c["commit"]["message"].split(":")[0]: c for c in commits}
    spec = [
        (411, "PAY-101", "Adds ApplePayTokenizer behind checkout.applepay.enabled.", "sosei", "2026-09-29T15:00:00Z"),
        (412, "PAY-104", "Dependency bump.", "liwei-dev", "2026-09-30T16:00:00Z"),
        (413, "PAY-103", "Complete message only after dispatcher retry policy.", "dreyes", "2026-10-01T12:00:00Z"),
        (414, "PAY-105", "Minor-unit aware rounding.", "sosei", "2026-10-02T18:00:00Z"),
        (415, "PAY-106", "capture.duration histogram.", "liwei-dev", "2026-10-03T14:00:00Z"),
        (416, "PAY-102", "Idempotency-Key support + additive migration.", "dreyes", "2026-10-05T17:00:00Z"),
        (417, "PAY-107", "Settlement batch moved to Functions.", "pnair", "2026-10-07T17:00:00Z"),
        (418, "PAY-108", "Null-safe default currency.", "pnair", "2026-10-08T12:00:00Z"),
    ]
    return [pull(n, by_msg[key], body, rev, at) for n, key, body, rev, at in spec]


def check_runs(conclusions: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "id": 7000 + i,
            "name": name,
            "status": "completed",
            "conclusion": conclusion,
            "app": {"slug": "github-actions"},
            "completed_at": "2026-10-08T12:20:00Z",
        }
        for i, (name, conclusion) in enumerate(conclusions.items())
    ]


BASE_DEPENDABOT = [
    {
        "number": 31,
        "state": "open",
        "dependency": {"package": {"ecosystem": "nuget", "name": "Acme.Shared.Logging"}, "manifest_path": f"{API}/Payments.Api.csproj"},
        "security_advisory": {
            "ghsa_id": "GHSA-7x2q-m4rp-acm1",
            "cve_id": "CVE-2026-40117",
            "summary": "Log forging via unescaped newline characters in structured log templates",
            "severity": "medium",
            "cvss": {"score": 5.3},
        },
        "security_vulnerability": {
            "vulnerable_version_range": "< 3.2.1",
            "first_patched_version": {"identifier": "3.2.1"},
        },
        "created_at": "2026-09-20T08:00:00Z",
    }
]

# -------------------------------------------------------------------------------------------- Azure

SITE_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Web/sites/{SITE}"
)


def deployment(slot: str, version: str, status: int, active: bool, start: str, end: str, message: str) -> dict[str, Any]:
    name = f"dep-{version}"
    slot_path = "" if slot == "production" else f"/slots/{slot}"
    return {
        "id": f"{SITE_ID}{slot_path}/deployments/{name}",
        "name": name,
        "type": "Microsoft.Web/sites/deployments",
        "properties": {
            "status": status,  # Kudu: 3 = failed, 4 = success
            "message": message,
            "author": "github-actions[bot]",
            "deployer": "GitHub Actions",
            "start_time": start,
            "end_time": end,
            "active": active,
        },
    }


def base_azure() -> dict[str, Any]:
    return {
        "site": {
            "id": SITE_ID,
            "name": SITE,
            "type": "Microsoft.Web/sites",
            "kind": "app",
            "location": LOCATION,
            "tags": {"service": REPO, "release": PREV_VERSION},
            "properties": {
                "state": "Running",
                "defaultHostName": f"{SITE}.azurewebsites.net",
                "httpsOnly": True,
                "resourceGroup": RESOURCE_GROUP,
                "siteConfig": {"netFrameworkVersion": "v8.0", "alwaysOn": True, "healthCheckPath": "/health"},
            },
        },
        "slots": {
            "staging": {
                "id": f"{SITE_ID}/slots/staging",
                "name": f"{SITE}/staging",
                "type": "Microsoft.Web/sites/slots",
                "location": LOCATION,
                "tags": {"service": REPO, "release": f"{FIX_VERSION}-rc.1"},
                "properties": {"state": "Running", "defaultHostName": f"{SITE}-staging.azurewebsites.net"},
            }
        },
        "deployments": {
            "production": [
                deployment("production", PREV_VERSION, 4, True, "2026-09-18T19:00:00Z", "2026-09-18T19:06:00Z", "Deploy v2.3.1"),
                deployment("production", "2.3.0", 4, False, "2026-09-02T19:00:00Z", "2026-09-02T19:05:30Z", "Deploy v2.3.0"),
            ],
            "staging": [
                deployment("staging", f"{FIX_VERSION}-rc.1", 4, True, "2026-10-08T13:00:00Z", "2026-10-08T13:07:00Z", "Deploy v2.4.0-rc.1"),
                deployment("staging", PREV_VERSION, 4, False, "2026-09-17T13:00:00Z", "2026-09-17T13:05:00Z", "Deploy v2.3.1"),
            ],
        },
        # Which release, if any, misbehaves under production load (see the ``failed_deploy`` scenario).
        "bad_release": None,
        "swaps": [],
    }


# -------------------------------------------------------------------------------------------- Sonar


def sonar_measures(**values: float) -> dict[str, str]:
    return {k: str(v) for k, v in values.items()}


def base_sonar() -> dict[str, Any]:
    return {
        "component": {"key": REPO, "name": "Payments API", "qualifier": "TRK"},
        "branches": {
            f"release/{PREV_VERSION}": sonar_measures(
                coverage=83.9, new_coverage=86.5, bugs=0, vulnerabilities=0, security_hotspots=1,
                code_smells=35, duplicated_lines_density=2.0, ncloc=47120, tests=1318, test_failures=0,
            ),
            RELEASE_BRANCH: sonar_measures(
                coverage=84.2, new_coverage=88.0, bugs=0, vulnerabilities=0, security_hotspots=1,
                code_smells=37, duplicated_lines_density=2.1, ncloc=48210, tests=1402, test_failures=0,
            ),
        },
        "quality_gate": {
            # Conditions on new code; actual values are filled from the branch's measures.
            "conditions": [
                {"metricKey": "new_coverage", "comparator": "LT", "errorThreshold": "80"},
                {"metricKey": "vulnerabilities", "comparator": "GT", "errorThreshold": "0"},
                {"metricKey": "bugs", "comparator": "GT", "errorThreshold": "0"},
            ]
        },
    }


# ---------------------------------------------------------------------------------------- ServiceNow


def base_servicenow() -> dict[str, Any]:
    users = [
        {"sys_id": "u-dana", "user_name": "dana.reyes", "name": "Dana Reyes", "email": "dana.reyes@acme-fin.test", "title": "Engineering Manager, Payments"},
        {"sys_id": "u-marcus", "user_name": "marcus.bell", "name": "Marcus Bell", "email": "marcus.bell@acme-fin.test", "title": "CAB Chair"},
        {"sys_id": "u-ines", "user_name": "ines.ferreira", "name": "Ines Ferreira", "email": "ines.ferreira@acme-fin.test", "title": "Application Security Lead"},
        {"sys_id": "u-tom", "user_name": "tom.hughes", "name": "Tom Hughes", "email": "tom.hughes@acme-fin.test", "title": "Release Manager"},
    ]
    groups = [
        {"sys_id": "g-payments", "name": "Payments Engineering", "manager": "u-dana", "type": "assignment"},
        {"sys_id": "g-cab", "name": "Change Advisory Board", "manager": "u-marcus", "type": "approval"},
        {"sys_id": "g-appsec", "name": "Application Security", "manager": "u-ines", "type": "approval"},
        {"sys_id": "g-release", "name": "Release Management", "manager": "u-tom", "type": "approval"},
    ]
    ci = [
        {
            "sys_id": "ci-payments-api",
            "name": REPO,
            "business_criticality": "1 - most critical",
            "used_for": "Production",
            "support_group": "g-payments",
            "owned_by": "u-dana",
            "short_description": "Card and wallet payment capture, refunds and settlement for merchants",
        }
    ]
    changes = [
        {
            "sys_id": "chg-2-3-0",
            "number": "CHG0030012",
            "short_description": "Deploy payments-api 2.3.0 to production",
            "type": "normal",
            "risk": "3",  # 2 = high, 3 = moderate, 4 = low
            "impact": "2",
            "state": "3",
            "approval": "approved",
            "close_code": "unsuccessful",
            "close_notes": "Swap completed 19:05; Http5xx rose to 4% within 6 min due to a blocking EF migration lock on "
            "Captures. Backout executed (swap back) at 19:14. Lesson: run index migrations online before the swap.",
            "cmdb_ci": "ci-payments-api",
            "assignment_group": "g-payments",
            "correlation_id": "payments-api@2.3.0",
            "start_date": "2026-09-02 19:00:00",
            "end_date": "2026-09-02 20:00:00",
            "sys_created_on": "2026-08-30 10:00:00",
        },
        {
            "sys_id": "chg-2-3-1",
            "number": "CHG0030019",
            "short_description": "Deploy payments-api 2.3.1 to production",
            "type": "normal",
            "risk": "3",
            "impact": "2",
            "state": "3",
            "approval": "approved",
            "close_code": "successful",
            "close_notes": "Slot swap completed; health checks green for 30 min.",
            "cmdb_ci": "ci-payments-api",
            "assignment_group": "g-payments",
            "correlation_id": "payments-api@2.3.1",
            "start_date": "2026-09-18 19:00:00",
            "end_date": "2026-09-18 20:00:00",
            "sys_created_on": "2026-09-15 09:30:00",
        },
    ]
    return {
        "tables": {
            "sys_user": users,
            "sys_user_group": groups,
            "cmdb_ci_service": ci,
            "change_request": changes,
            "sysapproval_approver": [],
        },
        "next_change_number": 30027,
        "callback_url": None,
        "callback_log": [],
    }


# -------------------------------------------------------------------------------------------- World


def build_world() -> dict[str, Any]:
    commits = [dict(c) for c in BASE_COMMITS]
    head = commits[-1]["sha"]
    return {
        "jira": {
            "project": {"id": "10010", "key": JIRA_PROJECT, "name": "Payments API", "projectTypeKey": "software"},
            "versions": [
                {"id": "10231", "name": PREV_VERSION, "released": True, "releaseDate": "2026-09-18"},
                {"id": "10240", "name": FIX_VERSION, "released": False, "releaseDate": "2026-10-22"},
            ],
            "issues": {i["key"]: i for i in BASE_ISSUES},
        },
        "github": {
            "repo": {
                "id": 880011,
                "name": REPO,
                "full_name": REPO_FULL,
                "private": True,
                "default_branch": "main",
                "owner": {"login": ORG},
            },
            "commits": commits,  # linear history, oldest first
            "refs": {f"v{PREV_VERSION}": commits[0]["sha"], RELEASE_BRANCH: head, f"v{FIX_VERSION}-rc.1": head, "main": head},
            "pulls": base_pulls(commits),
            "check_runs": {head: check_runs({"build": "success", "unit-tests": "success", "integration-tests": "success", "CodeQL": "success"})},
            "dependabot_alerts": [dict(a) for a in BASE_DEPENDABOT],
            "code_scanning_alerts": [],
            "releases": [],
        },
        "azure": base_azure(),
        "sonar": base_sonar(),
        "servicenow": base_servicenow(),
    }
