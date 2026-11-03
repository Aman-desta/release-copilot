"""The release evidence model: everything known about one release, grouped by the worker that reasons about it.

Built from the integration layer's domain models (``Issue``, ``Commit``, ``CheckRun``...), not new copies of them:
there is one shape per fact, and every fact keeps its ``evidence_id``.

**Unknown is not empty.** Every section field is ``None`` when it could not be collected, and each ``None`` has a
matching ``EvidenceGap``. An empty tuple means "checked, and there are none": no open alerts, no PR for a commit.
Confusing the two is how a release passes review because the security scan *timed out*.
The exception is ``ServiceSection.existing_change``: ``None`` there is a normal state (no change request yet),
so a failed lookup is told apart only by its gap.
"""

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from release_copilot.integrations.models import (
    AppServiceSlot,
    ChangeRequest,
    CheckRun,
    CodeScanningAlert,
    Comparison,
    ConfigurationItem,
    DependencyAlert,
    Deployment,
    Issue,
    Measures,
    MetricSeries,
    PullRequest,
    QualityGate,
    Review,
    SnowGroup,
    comment_evidence_id,
)

# "agent": the gap comes from the copilot itself (a worker ran out of budget, or its answer was invalid), not a system.
Source = Literal["jira", "github", "sonar", "azure", "servicenow", "agent"]
GapKind = Literal["unavailable", "missing", "blocked", "exhausted"]

_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ------------------------------------------------------------------------------------------- identity


class ReleaseIdentity(_Frozen):
    """Which release this is, and the naming conventions that turn it into refs, versions and keys.

    The conventions live here only, so a team with other branch names changes one class, not every worker.
    """

    service: str = Field(description="Service name as registered in the CMDB, e.g. payments-api.")
    version: str = Field(description="The release being prepared, e.g. 2.4.0.")
    previous_version: str = Field(description="The release running in production now, e.g. 2.3.1. The diff baseline.")

    @field_validator("service")
    @classmethod
    def _service(cls, v: str) -> str:
        if not _SERVICE.fullmatch(v):
            raise ValueError("must be a lower-case service name like payments-api")
        return v

    @field_validator("version", "previous_version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not _VERSION.fullmatch(v):
            raise ValueError("must look like 2.4.0 or 2.4.0-rc.1")
        return v

    @property
    def fix_version(self) -> str:
        return self.version

    @property
    def release_branch(self) -> str:
        return f"release/{self.version}"

    @property
    def previous_branch(self) -> str:
        return f"release/{self.previous_version}"

    @property
    def base_tag(self) -> str:
        return f"v{self.previous_version}"

    @property
    def correlation_id(self) -> str:
        """The change request's idempotency key in ServiceNow (D11)."""
        return f"{self.service}@{self.version}"


# ----------------------------------------------------------------------------------------------- gaps


class EvidenceGap(_Frozen):
    """A piece of evidence that is missing, and why. Gaps are facts too: "coverage unknown" must reach the report."""

    source: Source
    section: str = Field(description="Dotted section path, e.g. quality.gate or change.commit_reviews.")
    subject: str | None = Field(default=None, description="The item the gap is about, e.g. gh:commit:1c63f6d.")
    kind: GapKind = Field(
        description="unavailable: the source failed · missing: the source has no such data · "
        "blocked: not attempted because evidence it depends on is missing · "
        "exhausted: a worker ran out of its model-call or tool-call budget, so its evidence may be incomplete."
    )
    error_type: str = Field(description="Integration error type, UnexpectedResponse, Blocked, BudgetExhausted or InvalidOutput.")
    message: str
    retryable: bool = False


# ------------------------------------------------------------------------------------------- sections


class ReviewedPull(_Frozen):
    pull: PullRequest
    reviews: tuple[Review, ...] | None  # None: the review lookup failed (see gaps)

    def latest_states(self) -> dict[str, str]:
        """Each reviewer's latest review state (GitHub lists reviews oldest first, so a later one replaces an earlier one)."""
        latest: dict[str, str] = {}
        for review in self.reviews or ():
            latest[review.reviewer] = review.state
        return latest

    @property
    def approved_by(self) -> tuple[str, ...]:
        return tuple(sorted(u for u, s in self.latest_states().items() if s == "APPROVED"))


class CommitReview(_Frozen):
    """The pull requests that contain one commit. ``pulls == ()`` means pushed directly, with no review."""

    commit_id: str  # evidence id of the commit, e.g. gh:commit:1c63f6d
    sha: str
    pulls: tuple[ReviewedPull, ...] | None  # None: the PR lookup failed (see gaps)


class TicketSection(_Frozen):
    """Jira worker: release scope and ticket hygiene."""

    issues: tuple[Issue, ...] | None = None


class ChangeSection(_Frozen):
    """Change worker: what changed since production, and whether every change was tracked and reviewed."""

    comparison: Comparison | None = None
    commit_reviews: tuple[CommitReview, ...] | None = None


class QualitySection(_Frozen):
    """Quality worker: CI, security alerts and code quality."""

    checks: tuple[CheckRun, ...] | None = None
    dependency_alerts: tuple[DependencyAlert, ...] | None = None
    code_scanning_alerts: tuple[CodeScanningAlert, ...] | None = None
    gate: QualityGate | None = None
    measures: Measures | None = None
    previous_measures: Measures | None = None  # the production release's branch, for "change since last release"


class DeploySection(_Frozen):
    """Deploy worker: what runs where, deployment history and baseline health."""

    slots: tuple[AppServiceSlot, ...] | None = None
    deployments: tuple[Deployment, ...] | None = None  # every slot, newest first per slot
    metrics: tuple[MetricSeries, ...] | None = None  # Requests, Http5xx, HttpResponseTime per slot


class ServiceSection(_Frozen):
    """ServiceNow context: how critical the service is, who supports it, and how past changes went."""

    ci: ConfigurationItem | None = None
    support_group: SnowGroup | None = None
    recent_changes: tuple[ChangeRequest, ...] | None = None
    existing_change: ChangeRequest | None = None  # this release's change request, if a previous run created one


class ReleaseEvidence(_Frozen):
    """All evidence for one release. Deterministic for the same upstream state, except ``collected_at``."""

    identity: ReleaseIdentity
    collected_at: datetime
    tickets: TicketSection = TicketSection()
    change: ChangeSection = ChangeSection()
    quality: QualitySection = QualitySection()
    deploy: DeploySection = DeploySection()
    service: ServiceSection = ServiceSection()
    gaps: tuple[EvidenceGap, ...] = ()

    def gaps_for(self, section: str) -> tuple[EvidenceGap, ...]:
        """Gaps in ``section`` or below it: ``gaps_for("quality")`` includes quality.gate."""
        return tuple(g for g in self.gaps if g.section == section or g.section.startswith(f"{section}."))


# ------------------------------------------------------------------------------------- evidence items


def iter_evidence(obj: Any) -> Iterator[tuple[str, str | None, BaseModel]]:
    """Every citable item inside ``obj`` as ``(evidence_id, url, item)``, depth first, in field order.

    Works on a ``ReleaseEvidence``, a section, one domain object, or a list of them (a tool artifact's
    ``objects``). Items without an ``evidence_id`` (reviews, changed files) are walked through, not yielded.
    Jira comments are yielded with their issue's URL, under ``comment_evidence_id``, the id the tools show.
    """
    if isinstance(obj, (list, tuple)):
        for item in obj:
            yield from iter_evidence(item)
        return
    if isinstance(obj, dict):
        for item in obj.values():
            yield from iter_evidence(item)
        return
    if not isinstance(obj, BaseModel):
        return
    evidence_id = getattr(obj, "evidence_id", None)
    if isinstance(evidence_id, str):
        yield evidence_id, getattr(obj, "url", None), obj
    if isinstance(obj, Issue):
        for comment in obj.comments:
            yield comment_evidence_id(obj.key, comment.id), obj.url, comment
        return
    for name in type(obj).model_fields:
        yield from iter_evidence(getattr(obj, name))


# ---------------------------------------------------------------------------------------------- schema


def _strip_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def evidence_json_schema() -> dict[str, Any]:
    """JSON schema of ``ReleaseEvidence`` for prompts and docs: field descriptions kept, auto-generated titles dropped."""
    return _strip_titles(ReleaseEvidence.model_json_schema(mode="serialization"))
