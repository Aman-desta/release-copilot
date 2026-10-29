"""GitHub read tools: traceability (commit -> PRs -> reviews), CI checks, security alerts, and the change set."""

import asyncio
import re
from typing import TYPE_CHECKING, Annotated

from langchain_core.tools import BaseTool
from pydantic import AfterValidator, Field

from release_copilot.integrations.models import ChangedFile
from release_copilot.tools.base import Evidence, ToolArgs, ToolPayload, View, make_tool

if TYPE_CHECKING:
    from release_copilot.tools.toolbox import Toolbox

def _no_parent_segments(value: str) -> str:
    if ".." in value:
        raise ValueError("must not contain '..'")
    return value


# A git ref the model may pass: branch, tag or sha. No spaces, no "..", nothing that could escape a path segment.
# (Pydantic's regex engine has no look-ahead, so ".." is checked by a validator.)
Ref = Annotated[
    str,
    Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._/\-]+$", examples=["release/2.4.0", "v2.3.1", "3f2a9c1"]),
    AfterValidator(_no_parent_segments),
]
Sha = Annotated[str, Field(min_length=7, max_length=40, pattern=r"^[0-9a-f]+$")]


# ------------------------------------------------------------------------------------- PRs for a commit


class CommitPullsArgs(ToolArgs):
    sha: Sha = Field(description="Commit sha (7-40 hex chars), e.g. from github_compare.")


class PullView(View):
    id: str
    number: int
    title: str
    author: str
    merged: bool
    approved_by: list[str] = []
    changes_requested_by: list[str] = []


class CommitPullsView(View):
    sha: str
    pull_requests: list[PullView]
    note: str | None = None


async def _commit_pulls(box: "Toolbox", args: CommitPullsArgs) -> ToolPayload:
    pulls = await box.github.pulls_for_commit(args.sha)
    reviews = await asyncio.gather(*(box.github.reviews(p.number) for p in pulls))
    views = []
    for pull, pull_reviews in zip(pulls, reviews):
        latest: dict[str, str] = {}
        for r in pull_reviews:  # a reviewer's latest review counts
            latest[r.reviewer] = r.state
        views.append(
            PullView(
                id=pull.evidence_id, number=pull.number, title=pull.title, author=pull.author, merged=pull.merged,
                approved_by=sorted(u for u, s in latest.items() if s == "APPROVED"),
                changes_requested_by=sorted(u for u, s in latest.items() if s == "CHANGES_REQUESTED"),
            )
        )
    note = None if pulls else "No pull request contains this commit: it was pushed directly (no review)."
    return ToolPayload(
        view=CommitPullsView(sha=args.sha[:7], pull_requests=views, note=note),
        evidence=[Evidence(p.evidence_id, p.url) for p in pulls],
        objects=[*pulls, *(r for rs in reviews for r in rs)],
    )


# ----------------------------------------------------------------------------------------- check runs


class ChecksArgs(ToolArgs):
    ref: Ref = Field(description="Branch, tag or sha whose CI checks to read, e.g. release/2.4.0.")


class CheckView(View):
    id: str
    name: str
    conclusion: str | None = None
    status: str


class ChecksView(View):
    ref: str
    all_passed: bool
    failing: list[str] = []
    checks: list[CheckView]


async def _checks(box: "Toolbox", args: ChecksArgs) -> ToolPayload:
    runs = await box.github.check_runs(args.ref)
    failing = [r.name for r in runs if r.conclusion not in ("success", "skipped", "neutral")]
    return ToolPayload(
        view=ChecksView(
            ref=args.ref,
            all_passed=bool(runs) and not failing,
            failing=failing,
            checks=[CheckView(id=r.evidence_id, name=r.name, conclusion=r.conclusion, status=r.status) for r in runs],
        ),
        evidence=[Evidence(r.evidence_id) for r in runs],
        objects=list(runs),
    )


# ----------------------------------------------------------------------------------- security alerts


class AlertsArgs(ToolArgs):
    ref: Ref = Field(description="Branch to read code-scanning alerts for, e.g. release/2.4.0.")


class DependencyAlertView(View):
    id: str
    severity: str
    package: str
    cve: str | None = None
    summary: str
    fixed_in: str | None = None


class CodeAlertView(View):
    id: str
    severity: str
    rule: str
    location: str
    message: str


class AlertsView(View):
    counts: dict[str, int]  # severity -> open alerts (dependency + code)
    dependency_alerts: list[DependencyAlertView]
    code_scanning_alerts: list[CodeAlertView]


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


async def _alerts(box: "Toolbox", args: AlertsArgs) -> ToolPayload:
    deps, code = await asyncio.gather(box.github.dependabot_alerts(state="open"), box.github.code_scanning_alerts(state="open", ref=args.ref))
    deps = sorted(deps, key=lambda a: _SEVERITY_ORDER.get(a.severity, 9))  # worst first: survives truncation
    code = sorted(code, key=lambda a: _SEVERITY_ORDER.get(a.severity, 9))
    counts: dict[str, int] = {}
    for sev in [a.severity for a in deps] + [a.severity for a in code]:
        counts[sev] = counts.get(sev, 0) + 1
    return ToolPayload(
        view=AlertsView(
            counts=counts,
            dependency_alerts=[
                DependencyAlertView(id=a.evidence_id, severity=a.severity, package=a.package, cve=a.cve_id, summary=a.summary, fixed_in=a.patched_version)
                for a in deps
            ],
            code_scanning_alerts=[
                CodeAlertView(id=a.evidence_id, severity=a.severity, rule=a.rule_id, location=f"{a.path}:{a.start_line}", message=a.message)
                for a in code
            ],
        ),
        evidence=[Evidence(a.evidence_id, a.url) for a in [*deps, *code]],
        objects=[*deps, *code],
    )


# ---------------------------------------------------------------------------- github_compare (YOUR PART)


class CompareArgs(ToolArgs):
    base: Ref = Field(description="The previous release tag, e.g. v2.3.1.")
    head: Ref = Field(description="The release branch or tag, e.g. release/2.4.0.")


class CommitSummaryView(View):
    id: str
    title: str
    ticket_keys: list[str]
    untracked: bool


class FileSummaryView(View):
    file_count: int
    additions: int
    deletions: int
    migrations: list[str]
    production_config: list[str]


class CompareView(View):
    id: str
    base: str
    head: str
    status: str
    ahead_by: int
    commits: list[CommitSummaryView]
    files: FileSummaryView


def _is_migration(file: ChangedFile) -> bool:
    path = file.filename.casefold()
    name = path.rsplit("/", 1)[-1]
    return (
        any(part in {"migration", "migrations"} for part in path.split("/"))
        or bool(re.match(r"^\d{8,14}[_-]", name))
    )


def _is_production_config(file: ChangedFile) -> bool:
    path = file.filename.casefold()
    return bool(re.search(r"(?:^|[._/-])(?:prod|production)(?:[._/-]|$)", path))


async def _compare(box: "Toolbox", args: CompareArgs) -> ToolPayload:
    """Summarize commit traceability and the release-risk files, not the entire diff."""
    comparison = await box.github.compare(base=args.base, head=args.head)
    commits = [
        CommitSummaryView(
            id=commit.evidence_id,
            title=commit.title,
            ticket_keys=list(commit.ticket_keys),
            untracked=not commit.ticket_keys,
        )
        for commit in comparison.commits
    ]
    files = comparison.files
    return ToolPayload(
        view=CompareView(
            id=comparison.evidence_id,
            base=comparison.base,
            head=comparison.head,
            status=comparison.status,
            ahead_by=comparison.ahead_by,
            commits=commits,
            files=FileSummaryView(
                file_count=len(files),
                additions=sum(file.additions for file in files),
                deletions=sum(file.deletions for file in files),
                migrations=[file.filename for file in files if _is_migration(file)],
                production_config=[file.filename for file in files if _is_production_config(file)],
            ),
        ),
        evidence=[
            Evidence(comparison.evidence_id, comparison.url),
            *(Evidence(commit.evidence_id, commit.url) for commit in comparison.commits),
        ],
        objects=[comparison, *comparison.commits, *files],
    )


# --------------------------------------------------------------------------------------------- build


def build(box: "Toolbox") -> list[BaseTool]:
    return [
        make_tool(
            lambda a: _compare(box, a),
            name="github_compare",
            description=(
                "List what changed between two refs (previous release tag -> release branch): every commit with "
                "the Jira keys it references, plus a summary of changed files with migrations and config flagged."
            ),
            args_schema=CompareArgs, access="read", domain="github", budget=2000,
        ),
        make_tool(
            lambda a: _commit_pulls(box, a),
            name="github_commit_pull_requests",
            description=(
                "Find the pull requests that contain a commit and who approved them. Use it to check that every change "
                "was reviewed; an empty result means the commit was pushed without a pull request."
            ),
            args_schema=CommitPullsArgs, access="read", domain="github",
        ),
        make_tool(
            lambda a: _checks(box, a),
            name="github_check_runs",
            description="Read the CI check results (build, tests, CodeQL...) for a branch, tag or sha.",
            args_schema=ChecksArgs, access="read", domain="github",
        ),
        make_tool(
            lambda a: _alerts(box, a),
            name="github_security_alerts",
            description=(
                "List open security alerts: vulnerable dependencies (Dependabot, with CVE and fixed version) and "
                "code-scanning findings (CodeQL) on a branch, worst severity first."
            ),
            args_schema=AlertsArgs, access="read", domain="github",
        ),
    ]
