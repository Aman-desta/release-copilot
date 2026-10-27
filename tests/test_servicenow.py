"""Spec for ``Q`` and ``ServiceNowClient`` (story T4), against the mock Table API. Make these pass."""

from datetime import datetime, timezone

import pytest

from release_copilot.config import get_settings
from release_copilot.integrations.clients import servicenow_client
from release_copilot.integrations.errors import (
    ConflictError,
    InvalidRequestError,
    PolicyViolationError,
    UpstreamUnavailableError,
)
from release_copilot.integrations.models import ChangeRequestDraft
from release_copilot.integrations.servicenow import Q
from tests.integrations_support import Mocks, RewritingTransport

CI, GROUP = "ci-payments-api", "g-payments"
WINDOW = datetime(2026, 10, 22, 19, 0, tzinfo=timezone.utc)


def draft(**overrides) -> ChangeRequestDraft:
    values = {
        "short_description": "Deploy payments-api 2.4.0 to production",
        "cmdb_ci": CI,
        "assignment_group": GROUP,
        "risk": "3",
        "start_date": WINDOW,
        "correlation_id": "payments-api@2.4.0",
        "backout_plan": "Swap the slots back.",
    }
    return ChangeRequestDraft(**{**values, **overrides})


def client(mocks: Mocks, transport=None):
    return servicenow_client(get_settings(), transport=transport or mocks.transport())


def change_posts(mocks: Mocks) -> list[dict]:
    return [c for c in mocks.calls("/servicenow/api/now/table/change_request") if c["method"] == "POST"]


# --------------------------------------------------------------------------------------------- Q


def test_query_builder_encodes_conditions_in_order():
    q = Q().eq("cmdb_ci", CI).ne("state", "4").like("short_description", "payments").starts_with("number", "CHG")
    q = q.in_("risk", ["2", "3"]).order_by_desc("sys_created_on").order_by("number")
    assert str(q) == f"cmdb_ci={CI}^state!=4^short_descriptionLIKEpayments^numberSTARTSWITHCHG^riskIN2,3^ORDERBYDESCsys_created_on^ORDERBYnumber"
    assert Q().build() == ""


def test_query_builder_is_immutable():
    base = Q().eq("cmdb_ci", CI)
    narrowed = base.eq("state", "3")
    assert str(base) == f"cmdb_ci={CI}" and str(narrowed) == f"cmdb_ci={CI}^state=3"


def test_values_are_escaped_against_query_injection():
    # Without escaping, this value would add an OR branch that matches every record.
    assert str(Q().eq("short_description", "x^ORsys_id!=")) == "short_description=x^^ORsys_id!="


def test_invalid_field_names_and_values_are_rejected():
    with pytest.raises(ValueError):
        Q().eq("state^ORactive", "1")
    with pytest.raises(ValueError):
        Q().eq("Bad Field", "1")
    with pytest.raises(ValueError):
        Q().in_("risk", ["2,3"])  # commas can't be escaped inside IN
    with pytest.raises(ValueError):
        Q().in_("risk", [])
    assert str(Q().eq("cmdb_ci.name", "payments-api")) == "cmdb_ci.name=payments-api"  # dot-walking is allowed


async def test_escaping_works_end_to_end(mock_env):
    mocks = Mocks()
    async with client(mocks) as snow:
        created = await snow.create_change(draft(short_description="Fix a^ORb", correlation_id="x"))
        hits = await snow.find_changes(Q().eq("short_description", "Fix a^ORb"))
    assert [c.sys_id for c in hits] == [created.sys_id]


# ------------------------------------------------------------------------------------ table reads


async def test_records_page_through_everything_and_flatten_references(mock_env):
    mocks = Mocks()
    async with client(mocks) as snow:
        snow.page_size = 1
        rows = [r async for r in snow.records("change_request", Q().eq("cmdb_ci", CI))]
    assert len(rows) == 2 and rows[0]["assignment_group"] == GROUP  # sys_id, not {"link", "value"}
    calls = [c for c in mocks.calls("/servicenow/api/now/table/change_request")]
    assert len(calls) == 2 and all("sysparm_display_value=false" in c["query"] for c in calls)


async def test_references_without_links_are_handled(mock_env):
    def strip_links(request, body):  # what sysparm_exclude_reference_link=true returns
        for row in body.get("result", []) if isinstance(body.get("result"), list) else [body.get("result", {})]:
            for k, v in list(row.items()):
                if isinstance(v, dict) and "value" in v:
                    row[k] = v["value"]
        return body

    mocks = Mocks()
    async with client(mocks, RewritingTransport(mocks.transport(), strip_links)) as snow:
        [change] = await snow.find_changes(Q().eq("number", "CHG0030019"))
    assert change.assignment_group == GROUP


async def test_change_mapping(mock_env):
    async with client(Mocks()) as snow:
        changes = await snow.find_changes(Q().eq("cmdb_ci", CI).order_by_desc("sys_created_on"))
    assert [c.number for c in changes] == ["CHG0030019", "CHG0030012"]
    old = changes[1]
    assert (old.state_label, old.close_code, old.risk_label) == ("Closed", "unsuccessful", "Moderate")
    assert "migration lock" in old.close_notes  # the lesson long-term memory will learn from (P2)
    assert old.start_date == datetime(2026, 9, 2, 19, 0, tzinfo=timezone.utc)
    assert old.cmdb_ci == CI and old.evidence_id == "snow:change:CHG0030012"


async def test_find_by_correlation_id(mock_env):
    async with client(Mocks()) as snow:
        found = await snow.find_change_by_correlation_id("payments-api@2.3.1")
        missing = await snow.find_change_by_correlation_id("payments-api@9.9.9")
        await snow.create_change(draft(correlation_id="dup"))
        await snow.create_change(draft(correlation_id="dup"))
        with pytest.raises(ConflictError, match="dup"):
            await snow.find_change_by_correlation_id("dup")  # two records = a data problem; never pick one
    assert found is not None and found.number == "CHG0030019" and missing is None


# -------------------------------------------------------------------------------------------- writes


async def test_create_change(mock_env):
    async with client(Mocks()) as snow:
        change = await snow.create_change(draft())
        again = await snow.get_change(change.sys_id)
    assert change.number == "CHG0030027" and change.state_label == "New" and change.approval == "not requested"
    assert again == change and again.start_date == WINDOW and again.correlation_id == "payments-api@2.4.0"


async def test_create_is_not_retried(mock_env):
    mocks = Mocks()
    mocks.fault("/servicenow/api/now/table/change_request", status=503, method="POST", times=1)
    async with client(mocks) as snow:
        with pytest.raises(UpstreamUnavailableError):
            await snow.create_change(draft())
    assert len(change_posts(mocks)) == 1


async def test_business_rules_raise_policy_violation(mock_env):
    async with client(Mocks()) as snow:
        change = await snow.create_change(draft())
        with pytest.raises(PolicyViolationError, match="approver"):
            await snow.update_change(change.sys_id, approval="approved")  # the copilot can never self-approve
        with pytest.raises(PolicyViolationError, match="before it is approved"):
            await snow.update_change(change.sys_id, state="-2")
        updated = await snow.update_change(change.sys_id, description="Adds Apple Pay.")
    assert updated.description == "Adds Apple Pay."


async def test_invalid_values_are_invalid_requests(mock_env):
    async with client(Mocks()) as snow:
        change = await snow.create_change(draft())
        with pytest.raises(InvalidRequestError, match="risk"):
            await snow.update_change(change.sys_id, risk="9")


async def test_approval_lifecycle(mock_env):
    mocks = Mocks()
    async with client(mocks) as snow:
        change = await snow.create_change(draft())
        approval = await snow.request_approval(change.sys_id, "u-marcus", group_sys_id="g-cab")
        assert (approval.state, approval.change_sys_id, approval.approver_sys_id) == ("requested", change.sys_id, "u-marcus")
        assert (await snow.get_change(change.sys_id)).state_label == "Authorize"

        # A human approves (the mock's admin API plays the approver).
        from release_copilot_mocks.routers.servicenow import decide_approval

        decide_approval(mocks.state.world(), approval.sys_id, "approved", "CAB ok")
        [decided] = await snow.approvals(change.sys_id)
        after = await snow.get_change(change.sys_id)
    assert decided.state == "approved" and decided.comments == "CAB ok"
    assert (after.approval, after.state_label) == ("approved", "Scheduled")


# ---------------------------------------------------------------------------------------------- CMDB


async def test_cmdb_lookups(mock_env):
    async with client(Mocks()) as snow:
        ci = await snow.get_ci("payments-api")
        cab = await snow.get_group("Change Advisory Board")
        ines = await snow.get_user("u-ines")
        assert await snow.get_ci("nope") is None and await snow.get_group("Nope") is None and await snow.get_user("u-nope") is None
    assert ci is not None and ci.business_criticality == "1 - most critical" and ci.support_group_sys_id == GROUP
    assert cab is not None and cab.manager_sys_id == "u-marcus"
    assert ines is not None and ines.title == "Application Security Lead"


# ------------------------------------------------------------------------------------ review fixes


async def test_unordered_queries_get_a_stable_order(mock_env):
    from urllib.parse import unquote_plus

    mocks = Mocks()
    async with client(mocks) as snow:
        await snow.find_changes(Q().eq("cmdb_ci", CI))
        await snow.find_changes(Q().eq("cmdb_ci", CI).order_by_desc("sys_created_on"))
    first, second = (unquote_plus(c["query"]) for c in mocks.calls("/servicenow/api/now/table/change_request"))
    assert "ORDERBYsys_id" in first  # added: offset paging needs a deterministic order
    assert "ORDERBYsys_id" not in second and "ORDERBYDESCsys_created_on" in second  # the caller's order is kept


async def test_a_record_repeated_across_pages_is_yielded_once(mock_env):
    pages: dict[str, dict] = {}

    def table_changed_while_paging(request, body):
        # Page 2 repeats page 1's record, as happens when a row is inserted ahead of it between requests.
        offset = request.url.params.get("sysparm_offset")
        if "change_request" in request.url.path and offset == "0":
            pages["first"] = body["result"][0]
        elif "change_request" in request.url.path and offset == "1":
            body["result"] = [pages["first"]]
        return body

    mocks = Mocks()
    async with client(mocks, RewritingTransport(mocks.transport(), table_changed_while_paging)) as snow:
        snow.page_size = 1
        rows = [r async for r in snow.records("change_request", Q().eq("cmdb_ci", CI))]
    assert len(rows) == len({r["sys_id"] for r in rows}) == 1


async def test_raw_string_queries_are_rejected(mock_env):
    async with client(Mocks()) as snow:
        with pytest.raises(TypeError, match="Q"):
            await snow.find_changes("number=CHG0030019")  # type: ignore[arg-type]


async def test_request_approval_is_idempotent(mock_env):
    mocks = Mocks()
    async with client(mocks) as snow:
        change = await snow.create_change(draft())
        first = await snow.request_approval(change.sys_id, "u-marcus", group_sys_id="g-cab")
        again = await snow.request_approval(change.sys_id, "u-marcus", group_sys_id="g-cab")
        other = await snow.request_approval(change.sys_id, "u-ines", group_sys_id="g-appsec")
    assert again.sys_id == first.sys_id and other.sys_id != first.sys_id
    assert len(mocks.state.world()["servicenow"]["tables"]["sysapproval_approver"]) == 2
