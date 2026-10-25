"""Domain models for evidence: only the fields agents need, independent of vendor JSON shapes.

Clients map vendor responses onto these. Each model that becomes evidence has an ``evidence_id``: a stable,
human-readable reference that the release document cites and the validator checks (T5, A4).
"""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

TICKET_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def parse_jira_datetime(value: str | datetime | None) -> datetime | None:
    """Jira sends ``2026-10-08T16:30:00.000+0000`` (no colon in the offset), which ISO 8601 parsers reject."""
    if value is None or isinstance(value, datetime):
        return value
    if re.search(r"[+-]\d{4}$", value):
        value = f"{value[:-2]}:{value[-2:]}"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ------------------------------------------------------------------------------------------------ Jira


class JiraComment(_Model):
    id: str
    author: str
    body_markdown: str
    created: datetime

    _parse_created = field_validator("created", mode="before")(lambda cls, v: parse_jira_datetime(v))


class Issue(_Model):
    key: str
    summary: str
    issue_type: str
    status: str
    status_category: Literal["new", "indeterminate", "done"]
    priority: str | None = None
    assignee: str | None = None
    labels: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    fix_versions: tuple[str, ...] = ()
    description_markdown: str = ""
    comments: tuple[JiraComment, ...] = ()
    created: datetime
    updated: datetime
    resolved: datetime | None = None
    url: str

    _parse_dates = field_validator("created", "updated", "resolved", mode="before")(lambda cls, v: parse_jira_datetime(v))

    @property
    def evidence_id(self) -> str:
        return f"jira:{self.key}"

    @property
    def is_done(self) -> bool:
        return self.status_category == "done"


# ---------------------------------------------------------------------------------------------- GitHub


class ChangedFile(_Model):
    filename: str
    status: str  # added | modified | removed | renamed | ...
    additions: int
    deletions: int


class Commit(_Model):
    sha: str
    message: str
    author_name: str
    author_login: str | None = None
    date: datetime
    url: str
    files: tuple[ChangedFile, ...] | None = None  # only when fetched individually

    @property
    def short_sha(self) -> str:
        return self.sha[:7]

    @property
    def title(self) -> str:
        return self.message.splitlines()[0] if self.message else ""

    @property
    def ticket_keys(self) -> tuple[str, ...]:
        """Jira keys referenced in the message (the traceability link between code and tickets)."""
        return tuple(dict.fromkeys(TICKET_KEY.findall(self.message)))

    @property
    def evidence_id(self) -> str:
        return f"gh:commit:{self.short_sha}"


class Comparison(_Model):
    base: str
    head: str
    status: str
    ahead_by: int
    total_commits: int
    commits: tuple[Commit, ...]
    files: tuple[ChangedFile, ...]
    url: str

    @property
    def evidence_id(self) -> str:
        return f"gh:compare:{self.base}...{self.head}"


class PullRequest(_Model):
    number: int
    title: str
    state: str
    merged: bool
    merged_at: datetime | None = None
    author: str
    base_ref: str
    head_ref: str
    merge_commit_sha: str | None = None
    url: str

    @property
    def evidence_id(self) -> str:
        return f"gh:pr:{self.number}"


class Review(_Model):
    id: int
    reviewer: str
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED
    submitted_at: datetime | None = None


class CheckRun(_Model):
    name: str
    status: str
    conclusion: str | None = None  # success | failure | neutral | cancelled | skipped | timed_out | action_required
    head_sha: str
    completed_at: datetime | None = None

    @property
    def evidence_id(self) -> str:
        return f"gh:check:{self.name}@{self.head_sha[:7]}"


Severity = Literal["low", "medium", "high", "critical"]


class DependencyAlert(_Model):
    number: int
    state: str
    package: str
    ecosystem: str
    manifest_path: str
    severity: Severity
    cve_id: str | None = None
    ghsa_id: str
    summary: str
    cvss_score: float | None = None
    vulnerable_range: str
    patched_version: str | None = None
    url: str

    @property
    def evidence_id(self) -> str:
        return f"gh:dependabot:{self.number}"


class CodeScanningAlert(_Model):
    number: int
    state: str
    rule_id: str
    severity: str  # security severity when present (low..critical), else the rule severity (note/warning/error)
    description: str
    tool: str
    path: str
    start_line: int
    ref: str
    message: str
    url: str

    @property
    def evidence_id(self) -> str:
        return f"gh:code-scanning:{self.number}"


class Release(_Model):
    id: int
    tag_name: str
    name: str
    draft: bool
    prerelease: bool
    url: str


# ----------------------------------------------------------------------------------------------- Azure

# Kudu deployment status codes, as App Service reports them.
KUDU_STATUS = {0: "pending", 1: "building", 2: "deploying", 3: "failed", 4: "success"}
DeploymentStatus = Literal["pending", "building", "deploying", "failed", "success", "unknown"]


class AppServiceSlot(_Model):
    """The production site or a deployment slot. ``release`` comes from the resource's ``release`` tag."""

    site: str
    slot: str  # "production" for the site itself
    state: str  # Running | Stopped
    host_name: str
    release: str | None = None

    @property
    def evidence_id(self) -> str:
        return f"azure:slot:{self.slot}"


class Deployment(_Model):
    id: str
    slot: str
    status: DeploymentStatus
    active: bool
    message: str
    author: str
    deployer: str
    started: datetime | None = None
    ended: datetime | None = None

    @property
    def evidence_id(self) -> str:
        return f"azure:deploy:{self.slot}:{self.id}"


class MetricPoint(_Model):
    timestamp: datetime
    value: float | None = None  # None = no data in that interval


class MetricSeries(_Model):
    name: str  # e.g. Http5xx
    slot: str
    unit: str  # Count | Seconds | ...
    aggregation: str  # total | average | ...
    interval: str  # ISO 8601 duration, e.g. PT5M
    points: tuple[MetricPoint, ...]

    @property
    def values(self) -> list[float]:
        return [p.value for p in self.points if p.value is not None]

    @property
    def latest(self) -> float | None:
        return next((p.value for p in reversed(self.points) if p.value is not None), None)

    def sum(self, last: int | None = None) -> float:
        return sum(self.values[-last:] if last else self.values)

    @property
    def evidence_id(self) -> str:
        return f"azure:metric:{self.slot}:{self.name}"


def error_rate(errors: MetricSeries, requests: MetricSeries, *, last: int = 1) -> float | None:
    """Share of requests that failed over the last ``last`` intervals (e.g. Http5xx / Requests); None without traffic."""
    total = requests.sum(last)
    return None if total <= 0 else errors.sum(last) / total


SwapStatus = Literal["Succeeded", "AlreadyInPlace"]


class SwapResult(_Model):
    source: str
    target: str
    status: SwapStatus
    target_release_before: str | None
    target_release_after: str | None
    operation_id: str | None = None  # None when no swap was needed
    polls: int = 0
