"""Jira tools: the release scope, one issue in detail, and posting a comment (the only model-callable write).

YOUR PART. The contract is in tests/test_tools.py (the ``jira_*`` tests). Design your own argument schemas
(subclass ``ToolArgs``) and views (subclass ``View``); each tool function returns a ``ToolPayload``.

Required tools (names are part of the contract):
- ``jira_release_issues(fix_version)``: every issue in the fix version, compact: no descriptions or comments.
  The 8-ticket release must fit in a 2,000-token budget. Include enough to spot problems: status (and which
  issues are not done), type, priority, assignee, labels, components.
- ``jira_get_issue(key)``: one issue in full: description and comments as Markdown.
- ``jira_add_comment(key, text, idempotency_key)``: access="write". Posting twice with the same key must
  leave ONE comment: Jira has no idempotency keys, so append a visible audit reference and check for it first.

Security: ``fix_version`` and ``key`` end up inside JQL or a URL. Validate them with strict patterns in the
argument schema, and quote the JQL value (JQL injection is the Jira version of what ``Q`` prevents).
"""

import hashlib
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool
from pydantic import Field

from release_copilot.tools.base import Evidence, ToolArgs, ToolPayload, View, make_tool

if TYPE_CHECKING:
    from release_copilot.tools.toolbox import Toolbox


class ReleaseIssuesArgs(ToolArgs):
    fix_version: str = Field(
        min_length=5,
        max_length=50,
        pattern=r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$",
        description='Fix version in major.minor.patch form, e.g. "2.4.0".',
    )


class IssueKeyArgs(ToolArgs):
    key: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$",
        description='Jira issue key, e.g. "PAY-102".',
    )


class AddCommentArgs(IssueKeyArgs):
    text: str = Field(min_length=1, max_length=32000, description="Markdown comment to add to the issue.")
    idempotency_key: str = Field(min_length=1, max_length=256, description="Stable key to prevent duplicate comments.")


class ReleaseIssueView(View):
    id: str
    summary: str
    status: str
    done: bool
    issue_type: str
    priority: str | None = None
    assignee: str | None = None
    labels: list[str]
    components: list[str]


class ReleaseIssuesView(View):
    issues: list[ReleaseIssueView]


class JiraCommentView(View):
    id: str
    author: str
    created: str
    body_markdown: str


class IssueView(View):
    id: str
    key: str
    summary: str
    issue_type: str
    status: str
    status_category: str
    priority: str | None = None
    assignee: str | None = None
    labels: list[str]
    components: list[str]
    description_markdown: str
    comments: list[JiraCommentView]


class AddedCommentView(View):
    id: str
    key: str
    author: str
    created: str
    body_markdown: str


RELEASE_FIELDS = ("summary", "issuetype", "status", "priority", "assignee", "labels", "components", "created", "updated")


def _comment_marker(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    return f"— posted by release-copilot · ref {digest[:16]}"


def _comment_id(key: str, comment_id: str) -> str:
    return f"jira:comment:{key}:{comment_id}"


async def _release_issues(box: "Toolbox", args: ReleaseIssuesArgs) -> ToolPayload:
    issues = [
        issue
        async for issue in box.jira.search(
            f'fixVersion = "{args.fix_version}" ORDER BY key ASC', fields=RELEASE_FIELDS
        )
    ]
    return ToolPayload(
        view=ReleaseIssuesView(
            issues=[
                ReleaseIssueView(
                    id=issue.evidence_id,
                    summary=issue.summary,
                    status=issue.status,
                    done=issue.is_done,
                    issue_type=issue.issue_type,
                    priority=issue.priority,
                    assignee=issue.assignee,
                    labels=list(issue.labels),
                    components=list(issue.components),
                )
                for issue in issues
            ]
        ),
        evidence=[Evidence(issue.evidence_id, issue.url) for issue in issues],
        objects=list(issues),
    )


async def _get_issue(box: "Toolbox", args: IssueKeyArgs) -> ToolPayload:
    issue = await box.jira.get_issue(args.key)
    comments = [
        JiraCommentView(
            id=_comment_id(issue.key, comment.id),
            author=comment.author,
            created=comment.created.isoformat(),
            body_markdown=comment.body_markdown,
        )
        for comment in issue.comments
    ]
    return ToolPayload(
        view=IssueView(
            id=issue.evidence_id,
            key=issue.key,
            summary=issue.summary,
            issue_type=issue.issue_type,
            status=issue.status,
            status_category=issue.status_category,
            priority=issue.priority,
            assignee=issue.assignee,
            labels=list(issue.labels),
            components=list(issue.components),
            description_markdown=issue.description_markdown,
            comments=comments,
        ),
        evidence=[
            Evidence(issue.evidence_id, issue.url),
            *(Evidence(comment.id, issue.url) for comment in comments),
        ],
        objects=[issue],
    )


async def _add_comment(box: "Toolbox", args: AddCommentArgs) -> ToolPayload:
    marker = _comment_marker(args.idempotency_key)
    issue = await box.jira.get_issue(args.key)
    comment = next((comment for comment in issue.comments if marker in comment.body_markdown), None)
    if comment is None:
        comment = await box.jira.add_comment(args.key, f"{args.text.rstrip()}\n\n{marker}")

    body = comment.body_markdown
    if body.endswith(marker):
        body = body[: -len(marker)].rstrip()
    comment_id = _comment_id(issue.key, comment.id)
    return ToolPayload(
        view=AddedCommentView(
            id=comment_id,
            key=issue.key,
            author=comment.author,
            created=comment.created.isoformat(),
            body_markdown=body,
        ),
        evidence=[Evidence(comment_id, issue.url)],
        objects=[comment],
    )


def build(box: "Toolbox") -> list[BaseTool]:
    return [
        make_tool(
            lambda a: _release_issues(box, a),
            name="jira_release_issues",
            description=(
                "List every issue in a Jira fix version for release review. Shows summary, completion status, "
                "type, priority, assignee, labels and components; descriptions and comments are omitted."
            ),
            args_schema=ReleaseIssuesArgs,
            access="read",
            domain="jira",
            budget=2000,
        ),
        make_tool(
            lambda a: _get_issue(box, a),
            name="jira_get_issue",
            description="Read one Jira issue in full, including its description and comments as Markdown.",
            args_schema=IssueKeyArgs,
            access="read",
            domain="jira",
            budget=10000,
        ),
        make_tool(
            lambda a: _add_comment(box, a),
            name="jira_add_comment",
            description=(
                "Add a Markdown comment to a Jira issue with a short visible audit reference. "
                "Reusing the same idempotency key will not add another comment."
            ),
            args_schema=AddCommentArgs,
            access="write",
            domain="jira",
        ),
    ]
