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


# ------------------------------------------------------------------------------- completeness (review)

from tests.integrations_support import RewritingTransport  # noqa: E402


def rewritten(mocks: Mocks, rewrite):
    return jira_client(get_settings(), transport=RewritingTransport(mocks.transport(), rewrite))


async def test_get_issue_requests_only_mapped_fields(mock_env):
    mocks = Mocks()
    async with client(mocks) as jira:
        await jira.get_issue("PAY-102")
    assert "fields=" in mocks.calls("/jira/rest/api/3/issue/")[0]["query"]


async def test_missing_is_last_does_not_truncate_search(mock_env):
    def drop_is_last(request, body):
        body.pop("isLast", None) if request.url.path.endswith("/search/jql") else None
        return body

    async with rewritten(Mocks(), drop_is_last) as jira:
        issues = [i async for i in jira.search(FIX_VERSION, page_size=3)]
    assert len(issues) == 8


async def test_stuck_page_token_raises_instead_of_looping(mock_env):
    from release_copilot.integrations.errors import IntegrationError

    def stuck(request, body):
        if request.url.path.endswith("/search/jql"):
            body.update(isLast=False, nextPageToken="Mw==")  # a valid token ("offset 3") that never advances
        return body

    async with rewritten(Mocks(), stuck) as jira:
        with pytest.raises(IntegrationError, match="did not advance"):
            [i async for i in jira.search(FIX_VERSION, page_size=3)]


async def test_truncated_embedded_comments_are_fetched(mock_env):
    mocks = Mocks()
    world_issue = mocks.state.world()["jira"]["issues"]["PAY-102"]
    for n in range(1, 151):  # 151 comments in total: more than one comment page
        world_issue["fields"]["comment"]["comments"].append(
            {"id": str(90000 + n), "author": {"displayName": "Bot"}, "body": {"type": "doc", "content": []}, "created": "2026-10-09T10:00:00.000+0000"}
        )

    def embed_first_page_only(request, body):
        # Real Jira embeds only the first page of comments in issue payloads, plus the real total.
        for issue in body.get("issues", [body]):  # search pages and single issues
            comment = (issue.get("fields") or {}).get("comment")
            if isinstance(comment, dict) and "comments" in comment:
                comment.update(total=len(comment["comments"]), maxResults=20, comments=comment["comments"][:20])
        return body

    async with rewritten(mocks, embed_first_page_only) as jira:
        issue = await jira.get_issue("PAY-102")
        from_search = [i async for i in jira.search('key = PAY-102')]
    assert len(issue.comments) == 151 and len({c.id for c in issue.comments}) == 151
    assert issue.comments[0].author == "Dana Reyes"  # order kept: oldest first
    assert len(from_search[0].comments) == 151
    assert len(mocks.calls("/jira/rest/api/3/issue/PAY-102/comment")) == 4  # 2 pages each, for get and search
