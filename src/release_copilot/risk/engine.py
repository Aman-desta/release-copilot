"""The rules engine: signals + policy -> ``RiskAssessment``. Deterministic, and no model is involved.

The decision a release gets must be explainable as "rule X fired because of evidence Y", reproducible from the
same evidence and policy, and reviewable as a diff to a YAML file. That is why it is code, and why the model only
writes the rationale afterwards (``rationale.py``).
"""

from release_copilot.risk.model import TIER_RANK, Factor, RiskAssessment, Tier, max_tier  # noqa: F401  (for your implementation)
from release_copilot.risk.policy import ChangePolicy
from release_copilot.risk.signals import RiskSignals

# ------------------------------------------------------------------------------------------- YOUR PART


def evaluate_policy(signals: RiskSignals, policy: ChangePolicy) -> RiskAssessment:
    """Apply every rule of ``policy`` to ``signals`` (YOUR PART). The contract is in tests/test_risk.py (the
    ``evaluate_policy`` tests and the property tests).

    For each rule, in policy order, with ``signal = signals.get(rule.when.signal)``:

    1. **Does it fire?** If the signal is known (``signal.known``), it fires when ``rule.when.matches(signal.value)``.
       If it is unknown, it fires only when ``rule.on_unknown == "trigger"`` (fail closed), and its factor is
       marked ``unknown=True``.
    2. **The factor.** A fired rule becomes one ``Factor`` that copies the rule's effects (``rule_id``, ``reason``,
       ``points`` = ``rule.score``, ``block``, ``min_tier``, ``condition``) and the signal's ``evidence_ids``.

    Then:

    - ``factors``: every factor, in policy order. ``blockers``: the factors with ``block=True``, in the same order.
    - ``conditions``: the fired rules' ``condition`` texts, in policy order, without duplicates.
    - ``score``: the sum of the factors' points.
    - ``tier``: the **highest** of ``policy.tier_for_score(score)``, every factor's ``min_tier``, and
      ``policy.block_min_tier`` when there is a blocker. Nothing lowers a tier.
    - ``decision``: ``block`` if there is a blocker; else ``proceed_with_conditions`` if there is a condition;
      else ``proceed``.
    - ``policy_version``: ``policy.version``. The rationale fields stay at their defaults (``rationale.py`` fills them).

    The property tests check what must hold for *any* signals: making a signal worse never lowers the tier or
    turns a block into a proceed, and the result depends only on the signals and the policy.
    """
    factors: list[Factor] = []
    for rule in policy.rules:
        signal = signals.get(rule.when.signal)
        unknown = not signal.known
        if unknown:
            fires = rule.on_unknown == "trigger"
        else:
            fires = rule.when.matches(signal.value)

        if fires:
            factors.append(
                Factor(
                    rule_id=rule.id,
                    reason=rule.reason,
                    points=rule.score,
                    block=rule.block,
                    min_tier=rule.min_tier,
                    condition=rule.condition,
                    evidence_ids=signal.evidence_ids,
                    unknown=unknown,
                )
            )

    blockers = tuple(factor for factor in factors if factor.block)
    conditions = tuple(dict.fromkeys(factor.condition for factor in factors if factor.condition is not None))
    score = sum(factor.points for factor in factors)
    tiers: list[Tier] = [policy.tier_for_score(score)]
    tiers.extend(factor.min_tier for factor in factors if factor.min_tier is not None)
    if blockers:
        tiers.append(policy.block_min_tier)

    decision = "block" if blockers else "proceed_with_conditions" if conditions else "proceed"
    return RiskAssessment(
        tier=max_tier(*tiers),
        score=score,
        decision=decision,
        factors=tuple(factors),
        blockers=blockers,
        conditions=conditions,
        policy_version=policy.version,
    )
