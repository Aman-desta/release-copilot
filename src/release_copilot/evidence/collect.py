"""Deterministic evidence collection: every source for one release, fetched concurrently; failures become gaps.

No model is involved. This is the reference ("by hand") evidence for a release:
- A2's workers collect the same facts through tools, and their evidence is checked against this;
- evals use it as ground truth for what *could* have been known;
- it's the fallback when a worker runs out of budget.

One failing source never fails the collection: it becomes an ``EvidenceGap``, and everything that depended on
it becomes a "blocked" gap. Programming errors still raise, and cancel the other fetches (``TaskGroup``).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, TypeVar

from release_copilot.evidence.model import (
    ChangeSection,
    CommitReview,
    DeploySection,
    EvidenceGap,
    QualitySection,
    ReleaseEvidence,
    ReleaseIdentity,
    ReviewedPull,
    ServiceSection,
    Source,
    TicketSection,
)
from release_copilot.integrations.models import Commit, PullRequest, Review
from release_copilot.integrations.servicenow import Q

if TYPE_CHECKING:
    from release_copilot.tools.toolbox import Toolbox

log = logging.getLogger(__name__)
T = TypeVar("T")

SLOTS = ("production", "staging")
METRICS = ("Requests", "Http5xx", "HttpResponseTime")
RECENT_CHANGES = 20  # servicenow_service_profile's maximum, so anything that tool can cite is collected here
PR_CONCURRENCY = 8  # parallel GitHub calls for PR and review lookups (rate limits)


# ------------------------------------------------------------------------------------------- YOUR PART


async def capture(
    gaps: list[EvidenceGap],
    source: Source,
    section: str,
    fetch: Callable[[], Awaitable[T]],
    *,
    subject: str | None = None,
) -> T | None:
    """Run ``fetch()`` and return its result; if it fails, append an ``EvidenceGap`` to ``gaps`` and return None.

    YOUR PART. The contract is in tests/test_evidence.py (the ``capture`` and outage tests):
    - an ``IntegrationError`` becomes a gap: ``kind="missing"`` for ``NotFoundError``, otherwise ``"unavailable"``;
      ``error_type`` is the error's class name; ``message`` and ``retryable`` come from the error;
    - a response in a shape we don't understand (``KeyError``, ``ValidationError``, ``TypeError``, ``ValueError``)
      becomes a gap with ``error_type="UnexpectedResponse"``, ``kind="unavailable"`` and a generic message. Log the
      details; don't store them (the same rule as the tool layer, D19);
    - anything else is a bug and must propagate;
    - ``fetch`` is a zero-argument callable, so creating the coroutine happens inside ``capture`` too.
    """
    raise NotImplementedError("capture: your part of A1")


# ------------------------------------------------------------------------------------------- collection


async def resolve_identity(box: "Toolbox", *, service: str, version: str) -> ReleaseIdentity:
    """The release's identity, with the baseline taken from what production runs now (not the newest tag).

    The diff that matters is "what will change in production", and production may be behind the latest tag.
    """
    production = await box.azure.get_slot("production")
    if not production.release:
        raise ValueError("the production slot has no release tag, so the diff baseline is unknown")
    return ReleaseIdentity(service=service, version=version, previous_version=production.release)


async def collect_release_evidence(
    box: "Toolbox", identity: ReleaseIdentity, *, metrics_minutes: int = 60, now: datetime | None = None
) -> ReleaseEvidence:
    """Collect every section concurrently. Never raises for an upstream failure: see ``ReleaseEvidence.gaps``."""
    gaps: list[EvidenceGap] = []
    tickets, change, quality, deploy, service = await _all(
        _tickets(box, identity, gaps),
        _change(box, identity, gaps),
        _quality(box, identity, gaps),
        _deploy(box, gaps, metrics_minutes),
        _service(box, identity, gaps),
    )
    return ReleaseEvidence(
        identity=identity,
        collected_at=now or datetime.now(timezone.utc),
        tickets=tickets,
        change=change,
        quality=quality,
        deploy=deploy,
        service=service,
        gaps=tuple(sorted(gaps, key=lambda g: (g.section, g.subject or "", g.source, g.error_type))),
    )


async def _tickets(box: "Toolbox", identity: ReleaseIdentity, gaps: list[EvidenceGap]) -> TicketSection:
    # fix_version is validated by ReleaseIdentity, and quoted here as well (D20).
    jql = f'fixVersion = "{identity.fix_version}" ORDER BY key ASC'
    issues = await capture(gaps, "jira", "tickets.issues", lambda: _list(box.jira.search(jql)))
    return TicketSection(issues=_tuple(issues))


async def _change(box: "Toolbox", identity: ReleaseIdentity, gaps: list[EvidenceGap]) -> ChangeSection:
    comparison = await capture(
        gaps, "github", "change.comparison", lambda: box.github.compare(base=identity.base_tag, head=identity.release_branch)
    )
    if comparison is None:
        gaps.append(_blocked(gaps, "github", "change.commit_reviews", needs="change.comparison"))
        return ChangeSection()

    limit = asyncio.Semaphore(PR_CONCURRENCY)
    review_lookups: dict[int, asyncio.Task[list[Review] | None]] = {}  # one lookup per PR, shared by its commits

    async def limited(call: Callable[[], Awaitable[T]]) -> T:
        async with limit:
            return await call()

    def reviews_of(pull: PullRequest) -> "asyncio.Task[list[Review] | None]":
        if pull.number not in review_lookups:
            review_lookups[pull.number] = asyncio.create_task(
                capture(gaps, "github", "change.commit_reviews", lambda: limited(lambda: box.github.reviews(pull.number)), subject=pull.evidence_id)
            )
        return review_lookups[pull.number]

    async def review(commit: Commit) -> CommitReview:
        pulls = await capture(
            gaps, "github", "change.commit_reviews", lambda: limited(lambda: box.github.pulls_for_commit(commit.sha)), subject=commit.evidence_id
        )
        if pulls is None:
            return CommitReview(commit_id=commit.evidence_id, sha=commit.sha, pulls=None)
        reviews = await asyncio.gather(*(reviews_of(p) for p in pulls))
        reviewed = tuple(ReviewedPull(pull=p, reviews=_tuple(r)) for p, r in zip(pulls, reviews))
        return CommitReview(commit_id=commit.evidence_id, sha=commit.sha, pulls=reviewed)

    commit_reviews = await _all(*(review(c) for c in comparison.commits))
    return ChangeSection(comparison=comparison, commit_reviews=tuple(commit_reviews))


async def _quality(box: "Toolbox", identity: ReleaseIdentity, gaps: list[EvidenceGap]) -> QualitySection:
    branch = identity.release_branch
    checks, dependency_alerts, code_alerts, gate, measures, previous = await _all(
        capture(gaps, "github", "quality.checks", lambda: box.github.check_runs(branch)),
        capture(gaps, "github", "quality.dependency_alerts", lambda: box.github.dependabot_alerts(state="open")),
        capture(gaps, "github", "quality.code_scanning_alerts", lambda: box.github.code_scanning_alerts(state="open", ref=branch)),
        capture(gaps, "sonar", "quality.gate", lambda: box.sonar.quality_gate(branch)),
        capture(gaps, "sonar", "quality.measures", lambda: box.sonar.measures(branch)),
        capture(gaps, "sonar", "quality.previous_measures", lambda: box.sonar.measures(identity.previous_branch)),
    )
    return QualitySection(
        checks=_tuple(checks),
        dependency_alerts=_tuple(dependency_alerts),
        code_scanning_alerts=_tuple(code_alerts),
        gate=gate,
        measures=measures,
        previous_measures=previous,
    )


async def _deploy(box: "Toolbox", gaps: list[EvidenceGap], minutes: int) -> DeploySection:
    timespan = f"PT{minutes}M"
    slots, *per_slot = await _all(
        capture(gaps, "azure", "deploy.slots", box.azure.list_slots),
        *(capture(gaps, "azure", "deploy.deployments", lambda s=s: box.azure.deployments(s), subject=f"azure:slot:{s}") for s in SLOTS),
        *(
            capture(gaps, "azure", "deploy.metrics", lambda s=s: box.azure.metrics(list(METRICS), slot=s, timespan=timespan), subject=f"azure:slot:{s}")
            for s in SLOTS
        ),
    )
    deployments, metrics = per_slot[: len(SLOTS)], per_slot[len(SLOTS) :]
    return DeploySection(
        slots=_tuple(slots),
        # Per-slot lookups: a failed slot leaves a gap with that slot as subject, and the others are kept.
        # Only when every slot failed is the field unknown (None): an empty tuple would claim "no deployments".
        deployments=_partial(deployments, lambda found: tuple(d for ds in found for d in ds)),
        metrics=_partial(metrics, lambda found: tuple(ms[name] for ms in found for name in METRICS if name in ms)),
    )


async def _service(box: "Toolbox", identity: ReleaseIdentity, gaps: list[EvidenceGap]) -> ServiceSection:
    ci, existing = await _all(
        capture(gaps, "servicenow", "service.ci", lambda: box.servicenow.get_ci(identity.service)),
        capture(gaps, "servicenow", "service.existing_change", lambda: box.servicenow.find_change_by_correlation_id(identity.correlation_id)),
    )
    if ci is None:
        if not _has_gap(gaps, "service.ci"):  # the lookup worked: the service just isn't in the CMDB
            gaps.append(
                EvidenceGap(
                    source="servicenow", section="service.ci", kind="missing", error_type="NotFound",
                    message=f"no configuration item named {identity.service!r} in the CMDB",
                )
            )
        for section in ("service.support_group", "service.recent_changes"):
            gaps.append(_blocked(gaps, "servicenow", section, needs="service.ci"))
        return ServiceSection(existing_change=existing)

    query = Q().eq("cmdb_ci", ci.sys_id).order_by_desc("sys_created_on")
    group_id = ci.support_group_sys_id
    group, changes = await _all(
        capture(gaps, "servicenow", "service.support_group", lambda: box.servicenow.get_group_by_id(group_id)) if group_id else _none(),
        capture(gaps, "servicenow", "service.recent_changes", lambda: box.servicenow.find_changes(query)),
    )
    return ServiceSection(
        ci=ci,
        support_group=group,
        recent_changes=_tuple(changes[:RECENT_CHANGES] if changes is not None else None),
        existing_change=existing,
    )


# ---------------------------------------------------------------------------------------------- helpers


async def _all(*aws: Coroutine[Any, Any, Any]) -> list[Any]:
    """Run concurrently; a bug in one cancels the rest (unlike ``gather``, which leaves them running)."""
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(aw) for aw in aws]
    return [t.result() for t in tasks]


async def _list(items: Any) -> list[Any]:
    return [item async for item in items]


async def _none() -> None:
    return None


def _tuple(items: Any) -> tuple[Any, ...] | None:
    return None if items is None else tuple(items)


def _partial(results: list[Any], combine: Callable[[list[Any]], tuple[Any, ...]]) -> tuple[Any, ...] | None:
    found = [r for r in results if r is not None]
    return combine(found) if found else None


def _has_gap(gaps: list[EvidenceGap], section: str) -> bool:
    return any(g.section == section for g in gaps)


def _blocked(gaps: list[EvidenceGap], source: Source, section: str, *, needs: str) -> EvidenceGap:
    """A gap for evidence not attempted because ``needs`` is missing; retryable if the cause is."""
    cause = next((g for g in gaps if g.section == needs), None)
    return EvidenceGap(
        source=source, section=section, kind="blocked", error_type="Blocked",
        message=f"not collected: it needs {needs}, which is missing", retryable=bool(cause and cause.retryable),
    )
