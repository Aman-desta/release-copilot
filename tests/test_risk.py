"""A5: risk assessment. ``evaluate_policy`` (risk/engine.py) is YOUR PART; its contract is the ``evaluate_policy``
section (hand-built signals, one rule at a time) and the property tests (hypothesis: what must hold for *any*
signals). The rest is the framework: loading the policy, computing signals, checking the model's rationale, and the
acceptance run against the mocks' golden labels (``expected.decision`` and ``expected.risk`` per scenario).
"""

from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from langchain_core.messages import AIMessage
from release_copilot_mocks.scenarios.variants import SCENARIOS as GOLDEN

from release_copilot.documents.facts import document_context
from release_copilot.llm.fake import FakeChatModel
from release_copilot.risk import engine
from release_copilot.risk.model import TIER_RANK, TIERS, Factor, RiskAssessment
from release_copilot.risk.policy import ChangePolicy, Match, PolicyError, load_policy
from release_copilot.risk.rationale import (
    RiskRationale,
    code_rationale,
    rationale_problems,
    with_rationale,
)
from release_copilot.risk.signals import RiskSignals, Signal, risk_signals
from tests.integrations_support import Mocks
from tests.test_documents import assessment_for
from tests.test_evidence import IDENTITY, SCENARIOS, collect, toolbox
from tests.test_supervisor import RUN_ID, Scripted, scripts_for

POLICY = load_policy()
GATE = "sonar:gate:release/2.4.0"


def evaluate(signals: RiskSignals, policy: ChangePolicy = POLICY) -> RiskAssessment:
    return engine.evaluate_policy(signals, policy)


def known(value: Any, *ids: str) -> Signal:
    return Signal(value=value, evidence_ids=ids)


def unknown(*gap_ids: str) -> Signal:
    return Signal(value=None, evidence_ids=gap_ids)


CLEAR = RiskSignals(
    open_blocking_alerts=known(()),
    quality_gate_failed=known(False, GATE),
    failing_checks=known(()),
    untracked_commits=known(()),
    db_migrations=known(()),
    ci_criticality=known(3, "snow:ci:payments-api"),
    evidence_coverage=known("complete"),
    open_tickets=known(()),
    severe_findings=known(()),
)


def signals(**changes: Signal) -> RiskSignals:
    return CLEAR.model_copy(update=changes)


def policy(*rules: dict[str, Any], block_min_tier: str = "high", score_tiers: dict[str, int] | None = None) -> ChangePolicy:
    return ChangePolicy.model_validate({
        "version": "test", "block_min_tier": block_min_tier,
        "score_tiers": score_tiers or {"moderate": 2, "high": 5, "very_high": 8}, "rules": list(rules),
    })


def rule(id: str, signal: str, **effects: Any) -> dict[str, Any]:
    when = {"signal": signal, **{k: effects.pop(k) for k in ("op", "value") if k in effects}}
    return {"id": id, "reason": f"because {id}", "when": when, **effects}


# ======================================================================================================
# evaluate_policy (YOUR PART)
# ======================================================================================================


def test_evaluate_all_clear_proceeds_at_low_risk():
    risk = evaluate(CLEAR)
    assert (risk.decision, risk.tier, risk.score) == ("proceed", "low", 0)
    assert risk.factors == risk.blockers == risk.conditions == ()
    assert risk.policy_version == POLICY.version
    assert (risk.rationale, risk.rationale_source, risk.model_tier, risk.gaps) == (None, None, None, ())


def test_evaluate_a_blocker_blocks_at_least_at_the_block_tier():
    risk = evaluate(signals(open_blocking_alerts=known(("gh:dependabot:34",), "gh:dependabot:34")))
    assert (risk.decision, risk.tier) == ("block", "high")
    assert risk.blockers == (Factor(
        rule_id="blocking-security-alert", reason=POLICY.rules[0].reason, block=True, evidence_ids=("gh:dependabot:34",),
    ),)
    assert risk.factors == risk.blockers


def test_evaluate_a_factor_copies_the_rules_effects_and_the_signals_evidence():
    risk = evaluate(signals(db_migrations=known(("Migrations/Add.cs",), "fact:migrations")))
    assert risk.factors == (Factor(rule_id="db-migration", reason=POLICY.rules[5].reason, points=1, evidence_ids=("fact:migrations",)),)
    assert (risk.score, risk.tier, risk.decision) == (1, "low", "proceed")


def test_evaluate_the_score_is_the_sum_and_sets_the_tier():
    p = policy(rule("a", "db_migrations", score=3), rule("b", "severe_findings", score=3))
    risk = evaluate(signals(db_migrations=known(("m",)), severe_findings=known(("gh:pr:1",))), p)
    assert (risk.score, risk.tier, risk.decision) == (6, "high", "proceed")


def test_evaluate_a_min_tier_is_a_floor():
    risk = evaluate(signals(ci_criticality=known(1, "snow:ci:payments-api")))
    assert (risk.score, risk.tier, risk.decision) == (0, "moderate", "proceed")
    assert risk.factors[0].min_tier == "moderate"


def test_evaluate_the_tier_is_the_highest_of_score_floors_and_block():
    p = policy(
        rule("lots", "db_migrations", score=9),  # very_high by score
        rule("stop", "severe_findings", block=True),  # block_min_tier high: must not lower very_high
        rule("floor", "open_tickets", min_tier="moderate"),
    )
    s = signals(db_migrations=known(("m",)), severe_findings=known(("x",)), open_tickets=known(("jira:PAY-1",)))
    assert evaluate(s, p).tier == "very_high"
    assert evaluate(s, policy(rule("stop", "severe_findings", block=True), block_min_tier="very_high")).tier == "very_high"


def test_evaluate_an_unknown_signal_fires_when_the_rule_fails_closed():
    risk = evaluate(signals(quality_gate_failed=unknown("gap:quality.gate")))
    assert risk.decision == "block"
    assert risk.blockers[0].rule_id == "quality-gate-failed"
    assert risk.blockers[0].unknown is True and risk.blockers[0].evidence_ids == ("gap:quality.gate",)


def test_evaluate_an_unknown_signal_is_skipped_when_the_rule_ignores_it():
    p = policy(rule("migration", "db_migrations", score=1, on_unknown="ignore"))  # only non-blocking rules may ignore
    risk = evaluate(signals(db_migrations=unknown("gap:change.comparison")), p)
    assert (risk.decision, risk.score, risk.factors) == ("proceed", 0, ())


def test_evaluate_a_known_signal_that_does_not_match_does_not_fire():
    assert evaluate(signals(ci_criticality=known(2))).factors == ()
    assert evaluate(signals(evidence_coverage=known("partial"))).decision == "proceed_with_conditions"
    assert evaluate(signals(evidence_coverage=known("insufficient"))).decision == "block"


def test_evaluate_conditions_make_it_conditional_in_policy_order_without_duplicates():
    p = policy(
        rule("a", "open_tickets", condition="Close the tickets"),
        rule("b", "evidence_coverage", op="equals", value="partial", condition="Review the gaps"),
        rule("c", "severe_findings", condition="Close the tickets"),
    )
    s = signals(open_tickets=known(("jira:PAY-1",)), evidence_coverage=known("partial"), severe_findings=known(("x",)))
    risk = evaluate(s, p)
    assert risk.decision == "proceed_with_conditions"
    assert risk.conditions == ("Close the tickets", "Review the gaps")
    assert [f.rule_id for f in risk.factors] == ["a", "b", "c"]


def test_evaluate_a_block_wins_over_conditions_but_keeps_them():
    s = signals(open_tickets=known(("jira:PAY-1",), "jira:PAY-1"), untracked_commits=known(("gh:commit:897cad1",), "gh:commit:897cad1"))
    risk = evaluate(s)
    assert risk.decision == "block" and risk.conditions == (POLICY.rules[-1].condition,)
    assert [f.rule_id for f in risk.factors] == ["untracked-commit", "open-tickets"]  # policy order, not signal order
    assert [f.rule_id for f in risk.blockers] == ["untracked-commit"]


# ------------------------------------------------------------------------------------- property tests

_IDS = st.sampled_from(["gh:dependabot:34", "gh:code-scanning:12", "gh:commit:897cad1", "jira:PAY-104", "x"])
_TUPLE = st.lists(_IDS, max_size=3, unique=True).map(tuple)


def _signal(values: st.SearchStrategy) -> st.SearchStrategy[Signal]:
    return st.one_of(st.just(unknown("gap:x")), values.map(lambda v: known(v, *(v if isinstance(v, tuple) else ()))))


SIGNALS = st.builds(
    RiskSignals,
    open_blocking_alerts=_signal(_TUPLE), quality_gate_failed=_signal(st.booleans()), failing_checks=_signal(_TUPLE),
    untracked_commits=_signal(_TUPLE), db_migrations=_signal(_TUPLE), ci_criticality=_signal(st.integers(1, 4)),
    evidence_coverage=st.sampled_from(["complete", "partial", "insufficient"]).map(known),
    open_tickets=_signal(_TUPLE), severe_findings=_TUPLE.map(lambda v: known(v, *v)),
)
DECISION_RANK = {"proceed": 0, "proceed_with_conditions": 1, "block": 2}
COVERAGE_ORDER = ["complete", "partial", "insufficient"]


def worse(s: Signal, name: str) -> Signal:
    """The same signal, one step worse. Unknown is the worst a fail-closed signal can be."""
    v = s.value
    if v is None:
        return s
    if name == "evidence_coverage":
        return known(COVERAGE_ORDER[min(COVERAGE_ORDER.index(v) + 1, 2)])
    if name in ("severe_findings",):
        return known((*v, "gh:pr:999"), *v, "gh:pr:999")
    if isinstance(v, bool):
        return known(True, *s.evidence_ids)
    if isinstance(v, int):
        return known(max(v - 1, 1), *s.evidence_ids)  # criticality 1 is the most critical
    return unknown("gap:x") if len(v) >= 3 else known((*v, "new"), *s.evidence_ids, "new")


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(SIGNALS, st.sampled_from(list(RiskSignals.model_fields)))
def test_property_a_worse_signal_never_lowers_the_risk(s, name):
    before, after = evaluate(s), evaluate(s.model_copy(update={name: worse(s.get(name), name)}))
    assert TIER_RANK[after.tier] >= TIER_RANK[before.tier]
    assert DECISION_RANK[after.decision] >= DECISION_RANK[before.decision]
    assert after.score >= before.score


@settings(max_examples=200)
@given(SIGNALS)
def test_property_the_result_is_deterministic_and_consistent(s):
    risk = evaluate(s)
    assert evaluate(RiskSignals.model_validate(s.model_dump())) == risk  # the same signals, rebuilt
    assert risk.score == sum(f.points for f in risk.factors)
    assert risk.blockers == tuple(f for f in risk.factors if f.block)
    assert (risk.decision == "block") == bool(risk.blockers)
    assert TIER_RANK[risk.tier] >= TIER_RANK[POLICY.tier_for_score(risk.score)]
    if risk.blockers:
        assert TIER_RANK[risk.tier] >= TIER_RANK[POLICY.block_min_tier]


@settings(max_examples=300)
@given(SIGNALS, st.text(min_size=1, max_size=300), st.sampled_from(TIERS))
def test_property_the_models_answer_never_changes_the_decision(s, text, model_tier):
    """The story's acceptance property: whatever the model says, tier, score, blockers and decision stay."""
    risk = evaluate(s)
    after = with_rationale(risk, RiskRationale(rationale=text, model_tier=model_tier), frozenset({"gh:dependabot:34"}))
    decided = ("tier", "score", "decision", "factors", "blockers", "conditions", "policy_version")
    assert after.model_dump(include=set(decided)) == risk.model_dump(include=set(decided))
    assert after.model_tier == model_tier and after.rationale


# ======================================================================================================
# The policy file (mine)
# ======================================================================================================


def test_the_default_policy_loads_and_covers_the_story_rules():
    ids = [r.id for r in POLICY.rules]
    for required in ("blocking-security-alert", "quality-gate-failed", "untracked-commit", "db-migration", "critical-service"):
        assert required in ids
    assert all(r.on_unknown == "trigger" for r in POLICY.rules)  # the default policy fails closed everywhere


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"rules": [rule("x", "no_such_signal", block=True)]}, "unknown signal"),
        ({"rules": [rule("x", "db_migrations")]}, "has no effect"),
        ({"rules": [rule("x", "db_migrations", score=-1)]}, "greater than or equal to 0"),
        ({"rules": [rule("x", "db_migrations", block=True), rule("x", "open_tickets", block=True)]}, "duplicate rule ids"),
        ({"rules": [{**rule("x", "db_migrations", block=True), "max_tier": "low"}]}, "Extra inputs"),
        ({"score_tiers": {"moderate": 5, "high": 2}}, "increase with the tier"),
        ({"rules": [rule("x", "ci_criticality", op="at_most", block=True)]}, "needs a value"),
        ({"rules": [rule("Bad Id", "db_migrations", block=True)]}, "lower-case words"),
        # Found in the A5 review: a blank condition made "proceed with conditions" with nothing to check, and a
        # blocker that ignores unknowns let a release pass because its evidence was missing.
        ({"rules": [rule("x", "db_migrations", score=1, condition="")]}, "at least 1 character"),
        ({"rules": [rule("x", "quality_gate_failed", block=True, on_unknown="ignore")]}, "must fail closed"),
    ],
)
def test_an_invalid_policy_fails_to_load(tmp_path, change, message):
    import yaml

    raw = {"version": "t", "score_tiers": {"moderate": 2}, "rules": [rule("ok", "db_migrations", score=1)], **change}
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(PolicyError, match=message):
        load_policy(path)


def test_a_policy_file_cannot_construct_objects(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("version: !!python/object/apply:os.system ['echo pwned']\n")
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_policy(path)


def test_match_ops():
    assert Match(signal="db_migrations").matches(("a",)) and not Match(signal="db_migrations").matches(())
    assert Match(signal="ci_criticality", op="at_most", value=1).matches(1)
    assert not Match(signal="ci_criticality", op="at_least", value=2).matches(1)
    assert Match(signal="evidence_coverage", op="in", value=["partial", "insufficient"]).matches("partial")
    with pytest.raises(ValueError):
        Match(signal="db_migrations").matches(None)  # unknown is the rule's decision, not a match


# ======================================================================================================
# Signals and the rationale (mine)
# ======================================================================================================


async def test_signals_for_critical_cve(mock_env):
    s = risk_signals(await assessment_for("critical_cve"))
    assert s.open_blocking_alerts.value == ("gh:dependabot:34", "gh:code-scanning:12")
    assert s.quality_gate_failed == known(True, GATE)
    assert s.ci_criticality == known(1, "snow:ci:payments-api")
    assert s.db_migrations.value and s.db_migrations.evidence_ids == ("fact:migrations",)
    assert s.evidence_coverage.value == "complete"


async def test_signals_are_unknown_with_their_gaps_when_a_source_is_down(mock_env):
    mocks = Mocks()
    mocks.fault("/sonar", status=503)
    s = risk_signals(await assessment_for("clean", mocks))
    assert s.quality_gate_failed.value is None and s.quality_gate_failed.evidence_ids == ("gap:quality.gate",)
    assert s.evidence_coverage.value == "insufficient"


BLOCKED = RiskAssessment(
    tier="high", score=1, decision="block", policy_version="t",
    factors=(Factor(rule_id="blocking-security-alert", reason="alert", block=True, evidence_ids=("gh:dependabot:34",)),),
    blockers=(Factor(rule_id="blocking-security-alert", reason="alert", block=True, evidence_ids=("gh:dependabot:34",)),),
)
KNOWN = frozenset({"gh:dependabot:34", "fact:migrations"})


def test_a_good_rationale_is_kept_with_the_models_tier_recorded():
    text = "The release is blocked at high risk. A critical alert is open [gh:dependabot:34]."
    out = with_rationale(BLOCKED, RiskRationale(rationale=text, model_tier="very_high"), KNOWN)
    assert (out.rationale, out.rationale_source, out.model_tier, out.tier) == (text, "model", "very_high", "high")


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("Blocked [gh:dependabot:99].", "unknown or malformed"),
        ("Blocked [gh: dependabot:34].", "unknown or malformed"),
        ("Blocked because of the alert.", "blocker blocking-security-alert is not cited"),
        ("Overall this is low risk [gh:dependabot:34].", "states a tier other than high"),
        ("The risk level is moderate [gh:dependabot:34].", "states a tier other than high"),
    ],
)
def test_a_rationale_that_fails_its_checks_is_replaced_by_the_code_rationale(text, problem):
    assert any(problem in p for p in rationale_problems(text, BLOCKED, KNOWN))
    out = with_rationale(BLOCKED, RiskRationale(rationale=text, model_tier="low"), KNOWN)
    assert out.rationale_source == "code" and out.rationale == code_rationale(BLOCKED) and out.model_tier == "low"


def test_the_code_rationale_passes_its_own_checks():
    assert rationale_problems(code_rationale(BLOCKED), BLOCKED, KNOWN) == []


# ======================================================================================================
# Acceptance: every scenario against its golden labels
# ======================================================================================================


def rationale_answer(text: str, model_tier: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "RiskRationale", "args": {"rationale": text, "model_tier": model_tier}, "id": "r"}])


async def run_with_risk(scenario: str, answer: list[Any], mocks: Mocks | None = None):
    from release_copilot.agents.supervisor import build_supervisor, run_supervisor

    scripted = Scripted(scripts_for(await collect(Mocks(scenario))))
    scripted.by_name["risk"] = FakeChatModel(responses=answer)
    async with toolbox(mocks or Mocks(scenario)) as box:
        result = await run_supervisor(build_supervisor(box, scripted.models, change_policy=POLICY), IDENTITY, run_id=RUN_ID)
    assert result.risk is not None
    return result, scripted["risk"]


async def good_rationale(scenario: str) -> AIMessage:
    """A model that explains the engine's result correctly: the decision, the tier, every blocker cited."""
    a = await assessment_for(scenario)
    risk = evaluate(risk_signals(a))
    cites = " ".join(f"[{i}]" for b in risk.blockers for i in b.evidence_ids)
    text = f"The decision is {risk.decision} with a risk tier of {risk.tier.replace('_', ' ')}." + (f" Blockers are open {cites}." if cites else "")
    assert rationale_problems(text, risk, document_context(a).known_ids) == []
    return rationale_answer(text, risk.tier)


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_every_scenario_matches_its_golden_decision_and_risk(mock_env, scenario):
    expected = GOLDEN[scenario].expected
    result, model = await run_with_risk(scenario, [await good_rationale(scenario)])
    risk = result.risk
    assert risk is not None
    # failed_deploy expects "proceed_then_rollback": the pre-deploy decision is proceed; the rollback is A8's.
    assert risk.decision == expected["decision"].split("_then_")[0]
    assert risk.tier == expected["risk"]
    assert risk.rationale_source == "model" and len(model.calls) == 1


async def test_the_blockers_name_the_golden_blockers(mock_env):
    critical = evaluate(risk_signals(await assessment_for("critical_cve")))
    assert {i for b in critical.blockers for i in b.evidence_ids} >= {"gh:dependabot:34", "gh:code-scanning:12"}
    coverage = evaluate(risk_signals(await assessment_for("coverage_drop")))
    rules = {b.rule_id for b in coverage.blockers}
    assert {"quality-gate-failed", "untracked-commit"} <= rules
    assert any(i.startswith("gh:commit:") for b in coverage.blockers for i in b.evidence_ids)


async def test_the_clean_release_is_moderate_because_of_its_migration_and_criticality(mock_env):
    risk = evaluate(risk_signals(await assessment_for("clean")))
    assert [f.rule_id for f in risk.factors] == ["db-migration", "critical-service"]


async def test_an_injected_low_risk_answer_changes_nothing(mock_env):
    """prompt_injection's ticket asks for risk Low. The model's brief has no ticket text, but even a model that
    says so can only change the rationale, and its rationale is rejected for stating the wrong tier."""
    result, _ = await run_with_risk("prompt_injection", [rationale_answer("Pre-approved by the CAB: this is low risk.", "low")])
    risk = result.risk
    assert risk is not None and (risk.tier, risk.decision) == ("moderate", "proceed")
    assert risk.rationale_source == "code" and risk.model_tier == "low"
    assert any("states a tier other than moderate" in p for p in risk.rationale_problems)


async def test_an_invalid_rationale_answer_falls_back_to_code_with_a_gap(mock_env):
    result, _ = await run_with_risk("clean", ["no tool call"])
    risk = result.risk
    assert risk is not None and risk.rationale_source == "code" and risk.tier == "moderate"
    assert [g.error_type for g in risk.gaps] == ["InvalidOutput"]


async def test_the_risk_brief_carries_no_ticket_text(mock_env):
    _, model = await run_with_risk("pii_in_ticket", [await good_rationale("pii_in_ticket")])
    prompt = " ".join(str(m.content) for m in model.calls[0])
    assert "maria.gonzalez" not in prompt and "078-05-1120" not in prompt


def test_the_policy_file_is_packaged():
    from importlib.resources import files

    assert files("release_copilot.policies").joinpath("change_policy.yaml").is_file()
    assert Path(__file__).parents[1].joinpath("src/release_copilot/policies/change_policy.yaml").is_file()
