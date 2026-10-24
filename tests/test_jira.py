"""Spec for ``JiraClient`` (story T2), against the mocks. Make these pass."""

from datetime import timezone
from urllib.parse import unquote_plus

import pytest

from release_copilot.config import get_settings
from release_copilot.integrations.clients import jira_client
from release_copilot.integrations.errors import InvalidRequestError, NotFoundError
from tests.integrations_support import Mocks

FIX_VERSION = 'project = PAY AND fixVersion = "2.4.0" ORDER BY key ASC'
SEARCH = "/jira/rest/api/3/search/jql"


def client(mocks: Mocks):
    return jira_client(get_settings(), transport=mocks.transport())


async def test_search_follows_next_page_token(mock_env):
    mocks = Mocks()
    async with client(mocks) as jira:
        issues = [i async for i in jira.search(FIX_VERSION, page_size=3)]
    assert [i.key for i in issues] == [f"PAY-{n}" for n in range(101, 109)]
    calls = mocks.calls("/jira/rest/api/3/search")
    assert len(calls) == 3 and all(c["path"] == SEARCH for c in calls)  # the current endpoint, never the legacy one


async def test_search_requests_only_the_fields_it_maps(mock_env):
    mocks = Mocks()
    async with client(mocks) as jira:
        [_ async for _ in jira.search(FIX_VERSION)]
    query = unquote_plus(mocks.calls(SEARCH)[0]["query"])
    assert "fields=" in query and "comment" in query and "description" in query


async def test_issue_mapping(mock_env):
    async with client(Mocks()) as jira:
        issue = await jira.get_issue("PAY-102")
    assert issue.summary == "Idempotency keys for the payment capture endpoint"
    assert (issue.issue_type, issue.status, issue.status_category, issue.priority) == ("Story", "Done", "done", "High")
    assert issue.assignee == "Sam Osei"
    assert issue.labels == ("db-migration",) and issue.components == ("capture",) and issue.fix_versions == ("2.4.0",)
    assert "20261005120000_AddIdempotencyKey" in issue.description_markdown
    assert issue.comments[0].author == "Dana Reyes" and "online" in issue.comments[0].body_markdown
    assert issue.created.tzinfo is not None and issue.created.utcoffset() == timezone.utc.utcoffset(None)
    assert issue.resolved is not None
    assert issue.url.endswith("/browse/PAY-102") and issue.evidence_id == "jira:PAY-102"


async def test_search_and_get_agree(mock_env):
    async with client(Mocks()) as jira:
        from_search = {i.key: i async for i in jira.search(FIX_VERSION)}
        assert from_search["PAY-102"] == await jira.get_issue("PAY-102")


async def test_unknown_issue(mock_env):
    async with client(Mocks()) as jira:
        with pytest.raises(NotFoundError):
            await jira.get_issue("PAY-999")


async def test_add_comment_posts_adf_and_returns_markdown(mock_env):
    mocks = Mocks()
    text = "Released in **2.4.0** under change [CHG0030027](https://snow/CHG0030027)."
    async with client(mocks) as jira:
        comment = await jira.add_comment("PAY-102", text)
        issue = await jira.get_issue("PAY-102")
    stored = mocks.state.world()["jira"]["issues"]["PAY-102"]["fields"]["comment"]["comments"][-1]["body"]
    assert stored["type"] == "doc"  # Jira v3 rejects plain-text bodies
    assert comment.body_markdown == text and comment.author == "Release Copilot"
    assert issue.comments[-1].id == comment.id


async def test_transition_by_name(mock_env):
    async with client(Mocks()) as jira:
        await jira.transition("PAY-101", "released")  # case-insensitive
        assert (await jira.get_issue("PAY-101")).status == "Released"
        with pytest.raises(InvalidRequestError, match="Done"):  # the error lists what IS available
            await jira.transition("PAY-101", "Shipped")


async def test_prompt_injection_text_is_preserved_verbatim(mock_env):
    # The client must not "clean" content: detection is the guardrail layer's job (G1), and it needs the original.
    async with client(Mocks("prompt_injection")) as jira:
        issue = await jira.get_issue("PAY-109")
    assert "hooks.example.net" in issue.description_markdown
