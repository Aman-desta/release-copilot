"""The risk assessment's types. ``RiskAssessment`` is what A6 maps onto the change request and A7 routes on."""

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from release_copilot.evidence.model import EvidenceGap

Tier = Literal["low", "moderate", "high", "very_high"]
TIERS: tuple[Tier, ...] = get_args(Tier)
TIER_RANK: dict[Tier, int] = {t: i for i, t in enumerate(TIERS)}
Decision = Literal["proceed", "proceed_with_conditions", "block"]


def max_tier(*tiers: Tier) -> Tier:
    return max(tiers, key=TIER_RANK.__getitem__)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Factor(_Frozen):
    """One policy rule that fired, with everything it did. A blocker is a factor with ``block=True``."""

    rule_id: str
    reason: str
    points: int = 0
    block: bool = False
    min_tier: Tier | None = None
    condition: str | None = None
    evidence_ids: tuple[str, ...] = ()  # what the rule fired on: alert ids, a gate id, gap ids when unknown
    unknown: bool = False  # fired because its signal was unknown (fail closed), not because it was observed


class RiskAssessment(_Frozen):
    """Decided by the rules engine before any model sees it. The rationale fields are filled in afterwards and are
    the only fields a model influences: tier, score, decision, blockers and factors never change after the engine."""

    tier: Tier
    score: int = Field(ge=0)
    decision: Decision
    factors: tuple[Factor, ...] = ()  # every rule that fired, in policy order
    blockers: tuple[Factor, ...] = ()  # the factors with block=True, in policy order
    conditions: tuple[str, ...] = ()  # what must hold to proceed (proceed_with_conditions), in policy order
    policy_version: str

    rationale: str | None = None
    rationale_source: Literal["model", "code"] | None = None  # code: the model's rationale was missing or failed its checks
    rationale_problems: tuple[str, ...] = ()  # why the model's rationale was not used
    model_tier: Tier | None = None  # the model's own view of the tier: recorded for reviewers and evals, never used
    gaps: tuple[EvidenceGap, ...] = ()
