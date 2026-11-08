"""A3: the supervisor. Every worker gets its own scripted FakeChatModel (parallel workers would race for one
script), and the supervisor's model is scripted separately. The tests assert what the system does around the
models: which workers ran, what was merged, which follow-ups were accepted, what the budget stopped, and how the
policy reads the result.

``assess_coverage`` (policy.py) and ``accept_follow_ups`` (followups.py) are the spec; they're called through
their modules so the graph and the tests use the same function.
"""

import asyncio
import os
import warnings
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from release_copilot.agents import followups, policy
from release_copilot.agents import run as run_module
from release_copilot.agents.budget import BudgetExceeded, BudgetGuard, RunBudget
from release_copilot.agents.followups import FollowUpPlan, FollowUpRequest, WorkerTask
from release_copilot.agents.policy import (
    DEFAULT_POLICY,
    SECTIONS,
    Coverage,
    EvidencePolicy,
)
from release_copilot.agents.run import RunInfo, release_thread_id, run_config
from release_copilot.agents.supervisor import (
    SEVERITY_RANK,
    ReleaseAssessment,
    build_supervisor,
    checkpoint_serializer,
    default_models,
    run_supervisor,
)
from release_copilot.agents.workers import CHANGE, QUALITY, WORKERS
from release_copilot.evidence.index import EvidenceIndex
from release_copilot.evidence.model import EvidenceGap, ReleaseEvidence
from release_copilot.llm.fake import FakeChatModel
from release_copilot.llm.usage import UsageTracker
from tests.integrations_support import Mocks
from tests.test_agents import finish, script_for, turn
from tests.test_evidence import IDENTITY, SCENARIOS, collect, toolbox

RUN_ID = "run-a3"
CVE = "CVE-2026-41922"


# ------------------------------------------------------------------------------------------ scripting


class Scripted:
    """One FakeChatModel per worker, plus the supervisor's. ``models`` is what ``build_supervisor`` takes."""

    def __init__(self, replies: dict[str, list[Any]], supervisor: list[Any] | None = None) -> None:
        self.by_name: dict[str, FakeChatModel] = {name: FakeChatModel(responses=r) for name, r in replies.items()}
        self.by_name["supervisor"] = FakeChatModel(responses=supervisor or ["(no follow-ups expected)"])

    def models(self, name: str) -> BaseChatModel:
        return self.by_name[name]

    def __getitem__(self, name: str) -> FakeChatModel:
        return self.by_name[name]


def scripts_for(reference: ReleaseEvidence) -> dict[str, list[AIMessage]]:
    return {name: script_for(spec, reference)[0] for name, spec in WORKERS.items()}


def finding(title: str, severity: str, *ids: str) -> dict[str, Any]:
    return {"title": title, "severity": severity, "detail": "From the evidence.", "evidence_ids": list(ids)}


def answer_with(*findings: dict[str, Any], summary: str = "Checked.") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "WorkerOutput", "args": {"summary": summary, "findings": list(findings)}, "id": "answer"}])


def follow_up_plan(*requests: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "FollowUpPlan", "args": {"requests": list(requests)}, "id": "plan"}])


async def supervise(mocks: Mocks, scripted: Scripted, *, checkpointer=None, **kwargs: Any):
    async with toolbox(mocks) as box:
        graph = build_supervisor(box, scripted.models, checkpointer=checkpointer)
        return await run_supervisor(graph, IDENTITY, run_id=RUN_ID, **kwargs)


def cve_alert_id(reference: ReleaseEvidence) -> str:
    return next(a.evidence_id for a in reference.quality.dependency_alerts if a.cve_id == CVE)


def brief_of(model: FakeChatModel, call: int) -> str:
    return next(m.content for m in model.calls[call] if isinstance(m, HumanMessage))


# ------------------------------------------------------------------------------------- the policy (YOUR PART)


def evidence_with(*gaps: EvidenceGap) -> ReleaseEvidence:
    return ReleaseEvidence(identity=IDENTITY, collected_at=datetime(2026, 11, 6, tzinfo=UTC), gaps=gaps)


def gap(section: str, subject: str | None = None, source: str = "github") -> EvidenceGap:
    return EvidenceGap(source=source, section=section, subject=subject, kind="unavailable", error_type="ServiceUnavailable", message="down")  # type: ignore[arg-type]


ALL_COMPLETE = {name: "complete" for name in WORKERS}


def test_the_default_policy_classifies_every_section_once():
    assert DEFAULT_POLICY.required | DEFAULT_POLICY.optional == set(SECTIONS) and len(SECTIONS) == 16
    with pytest.raises(ValueError, match="unclassified"):
        EvidencePolicy(required=frozenset({"tickets.issues"}), optional=frozenset())
    with pytest.raises(ValueError, match="overlap"):
        EvidencePolicy(required=DEFAULT_POLICY.required, optional=DEFAULT_POLICY.optional | {"tickets.issues"})


def test_assess_coverage_complete():
    assert policy.assess_coverage(evidence_with(), ALL_COMPLETE) == Coverage(status="complete")


def test_assess_coverage_an_optional_gap_is_partial():
    coverage = policy.assess_coverage(evidence_with(gap("deploy.metrics", "azure:slot:staging", "azure")), ALL_COMPLETE)
    assert coverage == Coverage(status="partial", missing_optional=("deploy.metrics",))


def test_assess_coverage_a_gap_about_one_subject_still_makes_a_required_section_missing():
    coverage = policy.assess_coverage(evidence_with(gap("change.commit_reviews", "gh:commit:1c63f6d")), ALL_COMPLETE)
    assert coverage == Coverage(status="insufficient", missing_required=("change.commit_reviews",))


def test_assess_coverage_an_unknown_section_fails_closed():
    coverage = policy.assess_coverage(evidence_with(gap("quality.flaky_tests")), ALL_COMPLETE)
    assert coverage.status == "insufficient" and coverage.missing_required == ("quality.flaky_tests",)


def test_assess_coverage_an_incomplete_or_missing_worker_is_partial():
    workers = {"jira": "complete", "change": "exhausted", "quality": "no_answer", "deploy": None}
    coverage = policy.assess_coverage(evidence_with(), workers)
    assert coverage == Coverage(status="partial", incomplete_workers=("change", "deploy", "quality"))


def test_assess_coverage_reports_everything_sorted_and_once():
    ev = evidence_with(
        gap("quality.gate", source="sonar"), gap("tickets.issues", source="jira"),
        gap("deploy.metrics", "azure:slot:staging", "azure"), gap("deploy.metrics", "azure:slot:production", "azure"),
        gap("quality.measures", source="sonar"),
    )
    coverage = policy.assess_coverage(ev, {**ALL_COMPLETE, "quality": "exhausted"})
    assert coverage == Coverage(
        status="insufficient",
        missing_required=("quality.gate", "tickets.issues"),
        missing_optional=("deploy.metrics", "quality.measures"),
        incomplete_workers=("quality",),
    )


def test_assess_coverage_uses_the_policy_it_is_given():
    lenient = EvidencePolicy(required=DEFAULT_POLICY.required - {"quality.gate"}, optional=DEFAULT_POLICY.optional | {"quality.gate"})
    assert policy.assess_coverage(evidence_with(gap("quality.gate", source="sonar")), ALL_COMPLETE, lenient).status == "partial"


# --------------------------------------------------------------------------------- follow-ups (YOUR PART)


KNOWN = {"gh:alert:dep:7", "gh:commit:1c63f6d", "azure:slot:production", "jira:PAY-104"}
AVAILABLE = ("jira", "change", "quality", "deploy")


def request(worker: str, *ids: str, question: str = "Which commits changed it?") -> FollowUpRequest:
    return FollowUpRequest(worker=worker, question=question, evidence_ids=ids)


def test_accept_follow_ups_builds_tasks_for_the_next_round():
    plan = FollowUpPlan(requests=(request("change", "gh:alert:dep:7", "gh:alert:dep:7", question="  Which commits changed it?\n"),))
    decision = followups.accept_follow_ups(plan, known_ids=KNOWN, available=AVAILABLE, round=2)
    assert decision.accepted == (WorkerTask(worker="change", round=2, question="Which commits changed it?", about=("gh:alert:dep:7",)),)
    assert decision.rejected == ()


def test_accept_follow_ups_rejects_with_the_first_rule_broken():
    plan = FollowUpPlan(requests=(
        request("change", "gh:alert:dep:7", question="   "),             # empty_question
        request("security", "gh:alert:dep:7"),                          # worker_unavailable: no such worker
        request("change", "gh:alert:dep:7", "gh:alert:dep:999"),        # unknown_evidence: nobody collected it
        request("change", "gh:alert:dep:7"),                            # accepted
        request("change", "gh:commit:1c63f6d"),                         # duplicate_worker
        request("deploy", "azure:slot:production"),                     # accepted
        request("jira", "jira:PAY-104"),                                # over_limit: two already accepted
    ))
    decision = followups.accept_follow_ups(plan, known_ids=KNOWN, available=AVAILABLE, round=2)
    assert [t.worker for t in decision.accepted] == ["change", "deploy"]
    assert [r.reason for r in decision.rejected] == ["empty_question", "worker_unavailable", "unknown_evidence", "duplicate_worker", "over_limit"]
    assert [r.request for r in decision.rejected] == [plan.requests[i] for i in (0, 1, 2, 4, 6)]


def test_accept_follow_ups_a_rejected_request_does_not_use_a_slot():
    plan = FollowUpPlan(requests=(request("quality", "gh:alert:dep:999"), request("change", "gh:alert:dep:7"), request("deploy", "azure:slot:production")))
    decision = followups.accept_follow_ups(plan, known_ids=KNOWN, available=AVAILABLE, round=2, limit=2)
    assert [t.worker for t in decision.accepted] == ["change", "deploy"] and [r.reason for r in decision.rejected] == ["unknown_evidence"]


def test_accept_follow_ups_a_skipped_worker_is_unavailable():
    plan = FollowUpPlan(requests=(request("jira", "jira:PAY-104"),))
    decision = followups.accept_follow_ups(plan, known_ids=KNOWN, available=("change", "quality", "deploy"), round=2)
    assert decision.accepted == () and [r.reason for r in decision.rejected] == ["worker_unavailable"]


def test_accept_follow_ups_an_empty_plan():
    assert followups.accept_follow_ups(FollowUpPlan(), known_ids=KNOWN, available=AVAILABLE, round=2) == followups.FollowUpDecision()


# ------------------------------------------------------------------------------ end to end (acceptance)


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_an_end_to_end_run_per_scenario(mock_env, scenario):
    reference = await collect(Mocks(scenario))
    scripted = Scripted(scripts_for(reference))
    result = await supervise(Mocks(scenario), scripted)
    a = result.assessment

    assert a.evidence.model_dump(exclude={"collected_at"}) == reference.model_dump(exclude={"collected_at"})
    assert [(r.task.worker, r.task.round, r.report.status) for r in a.reports] == [(w, 1, "complete") for w in WORKERS]
    assert len(a.findings) == 4 and all(f.verified for f in a.findings)
    assert a.coverage == Coverage(status="complete") and a.gaps == () and a.follow_ups == ()
    assert len(scripted["supervisor"].calls) == 0  # no severe finding, so no follow-up call
    assert a.run == RunInfo(run_id=RUN_ID, thread_id=release_thread_id(IDENTITY, RUN_ID))
    assert result.usage["total_tokens"] > 0 and result.usage["total_cost_usd"] > 0
    for r in a.reports:  # the supervisor keeps ids, not the tools' raw output
        assert r.report.artifacts == () and set(r.collected) <= set(EvidenceIndex.from_evidence(a.evidence).ids)


async def test_the_workers_run_in_parallel(mock_env):
    reference = await collect(Mocks())
    barrier = asyncio.Barrier(len(WORKERS))

    class Gated(FakeChatModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if not self.calls:  # each worker's first call waits until all four are in a model call at once
                await barrier.wait()
            return self._generate(messages, stop, None, **kwargs)

    scripted = Scripted({})
    scripted.by_name |= {name: Gated(responses=replies) for name, replies in scripts_for(reference).items()}
    async with asyncio.timeout(10):
        result = await supervise(Mocks(), scripted)
    assert result.assessment.coverage.status == "complete"


async def test_the_supervisor_returns_only_the_assessment(mock_env):
    scripted = Scripted(scripts_for(await collect(Mocks())))
    async with toolbox(Mocks()) as box:
        out = await build_supervisor(box, scripted.models).ainvoke(
            {"identity": IDENTITY}, run_config(RUN_ID, "thread-1")
        )
    assert set(out) == {"assessment"} and isinstance(out["assessment"], ReleaseAssessment)


# ------------------------------------------------------------------------------------------- follow-ups


def critical_cve_scripts(reference: ReleaseEvidence, change_follow_up: list[AIMessage]) -> dict[str, list[AIMessage]]:
    scripts = scripts_for(reference)
    alert = cve_alert_id(reference)
    quality_turns = script_for(QUALITY, reference)[0][:-2]  # its tool turns, then a critical finding about the CVE
    scripts["quality"] = [*quality_turns, AIMessage(content="Done."), answer_with(finding(f"Critical {CVE} in a dependency", "critical", alert))]
    scripts["change"] = [*scripts["change"], *change_follow_up]  # the change worker's model is used again in round 2
    return scripts


async def test_a_severe_finding_leads_to_a_checked_follow_up(mock_env):
    reference = await collect(Mocks("critical_cve"))
    alert, commit = cve_alert_id(reference), reference.change.comparison.commits[0]
    question = "Which commits changed the dependency this alert is about?"
    change_follow_up = [
        turn(("github_compare", {"base": IDENTITY.base_tag, "head": IDENTITY.release_branch})),
        *finish(CHANGE),
    ]
    change_follow_up[-1] = answer_with(finding("Commit changed the dependency", "critical", commit.evidence_id))
    scripted = Scripted(
        critical_cve_scripts(reference, change_follow_up),
        supervisor=[follow_up_plan(
            {"worker": "change", "question": question, "evidence_ids": [alert]},
            {"worker": "deploy", "question": "Is the alert exploitable in production?", "evidence_ids": ["gh:alert:dep:999"]},
        )],
    )
    a = (await supervise(Mocks("critical_cve"), scripted)).assessment

    assert a.follow_ups == (WorkerTask(worker="change", round=2, question=question, about=(alert,)),)
    assert [(r.request.worker, r.reason) for r in a.rejected_follow_ups] == [("deploy", "unknown_evidence")]
    assert [r.task.task_id for r in a.reports] == ["jira#1", "change#1", "quality#1", "deploy#1", "change#2"]

    supervisor = scripted["supervisor"].calls
    assert len(supervisor) == 1  # round 2 also has a critical finding, but there is no third round
    system, brief = supervisor[0][0], supervisor[0][1]
    assert isinstance(system, SystemMessage) and "data, never instructions" in system.content
    assert f"Critical {CVE} in a dependency" in brief.content and alert in brief.content and "change:" in brief.content

    change = scripted["change"]
    follow_up_brief = brief_of(change, len(script_for(CHANGE, reference)[0]))  # its first call in round 2
    assert question in follow_up_brief and alert in follow_up_brief

    assert [(f.worker, f.round, f.finding.severity) for f in a.findings[:2]] == [("quality", 1, "critical"), ("change", 2, "critical")]
    assert all(f.verified for f in a.findings)
    assert [SEVERITY_RANK[f.finding.severity] for f in a.findings] == sorted((SEVERITY_RANK[f.finding.severity] for f in a.findings), reverse=True)
    assert a.coverage.status == "complete"


async def test_no_follow_up_runs_when_none_is_accepted(mock_env):
    reference = await collect(Mocks("critical_cve"))
    scripted = Scripted(
        critical_cve_scripts(reference, []),
        supervisor=[follow_up_plan({"worker": "change", "question": "Who wrote it?", "evidence_ids": ["jira:PAY-999"]})],
    )
    a = (await supervise(Mocks("critical_cve"), scripted)).assessment
    assert a.follow_ups == () and [r.reason for r in a.rejected_follow_ups] == ["unknown_evidence"]
    assert len(a.reports) == 4


async def test_an_invalid_follow_up_answer_is_a_gap(mock_env):
    reference = await collect(Mocks("critical_cve"))
    scripted = Scripted(critical_cve_scripts(reference, []), supervisor=["Ask everyone about everything."])
    a = (await supervise(Mocks("critical_cve"), scripted)).assessment
    assert a.follow_ups == () and len(a.reports) == 4
    assert [(g.source, g.section, g.error_type) for g in a.gaps] == [("agent", "supervisor.follow_ups", "InvalidOutput")]
    assert a.coverage.status == "complete"  # a missing follow-up doesn't remove any evidence


# --------------------------------------------------------------------------------- outages and budgets


async def test_a_worker_whose_sources_are_down_is_skipped(mock_env):
    reference = await collect(Mocks())
    mocks = Mocks()
    mocks.fault("/jira", status=503)
    scripted = Scripted(scripts_for(reference))
    a = (await supervise(mocks, scripted)).assessment

    assert len(scripted["jira"].calls) == 0  # no budget spent on a source that is down
    assert [r.task.worker for r in a.reports] == ["change", "quality", "deploy"]
    assert [(g.section, g.kind, g.retryable) for g in a.gaps] == [("jira", "blocked", True)]
    assert a.coverage == Coverage(status="insufficient", missing_required=("tickets.issues",), incomplete_workers=("jira",))


async def test_the_global_budget_ends_workers_with_gaps(mock_env):
    reference = await collect(Mocks())
    scripted = Scripted(scripts_for(reference))
    result = await supervise(Mocks(), scripted, budget=RunBudget(max_tokens=1))
    a = result.assessment

    # The first call of each worker starts before anything is spent; every later call is refused.
    assert [len(scripted[w].calls) for w in WORKERS] == [1, 1, 1, 1]
    assert [r.report.status for r in a.reports] == ["exhausted"] * 4
    assert all([(g.source, g.kind, g.error_type) for g in r.report.gaps] == [("agent", "exhausted", "BudgetExhausted")] for r in a.reports)
    assert a.coverage == Coverage(status="partial", incomplete_workers=tuple(sorted(WORKERS)))
    assert a.evidence.gaps == ()  # the facts don't depend on the model budget


def test_the_budget_guard():
    tracker = UsageTracker()
    BudgetGuard(RunBudget(max_tokens=100), tracker).check()  # nothing spent yet
    tracker._add("worker", "fake-model", 60, 40)
    with pytest.raises(BudgetExceeded, match="token budget"):
        BudgetGuard(RunBudget(max_tokens=100), tracker).check()
    with pytest.raises(BudgetExceeded, match="cost budget"):
        BudgetGuard(RunBudget(max_tokens=10_000, max_cost_usd=0.0001), tracker).check()
    tracker._add("worker", "mystery-model", 1, 1)  # no pricing: the cost is unknown
    with pytest.raises(BudgetExceeded, match="can't be checked"):
        BudgetGuard(RunBudget(max_tokens=10_000), tracker).check()
    BudgetGuard(RunBudget(max_tokens=10_000, max_cost_usd=None), tracker).check()  # no cost limit: tokens only
    with pytest.raises(ValueError):
        RunBudget(max_tokens=0)


# ----------------------------------------------------------------------------------- the run's identity


class NodeRecorder(BaseCallbackHandler):
    def __init__(self) -> None:
        self.seen: dict[str, set[tuple[Any, Any, Any]]] = {}

    def on_chain_start(self, serialized, inputs, *, metadata=None, **kwargs):
        metadata = metadata or {}
        if node := metadata.get("langgraph_node"):
            self.seen.setdefault(node, set()).add((metadata.get("run_id"), metadata.get("thread_id"), metadata.get("scenario")))


async def test_run_id_thread_id_and_scenario_reach_every_node_and_call(mock_env):
    reference = await collect(Mocks("critical_cve"))
    mocks = Mocks()  # the mock server defaults to "clean": the scenario comes from the run, per request
    recorder = NodeRecorder()
    result = await supervise(mocks, Scripted(scripts_for(reference)), scenario="critical_cve", thread_id="thread-7", callbacks=[recorder])

    assert result.assessment.run == RunInfo(run_id=RUN_ID, thread_id="thread-7", scenario="critical_cve")
    assert result.assessment.evidence.model_dump(exclude={"collected_at"}) == reference.model_dump(exclude={"collected_at"})
    # Every call, the collector's and the workers' alike. Not the Azure token request: the token is cached and
    # shared by every run, so that request belongs to no run (and carries neither header).
    calls = [r for r in mocks.calls() if not r["path"].startswith("/azure/login")]
    assert calls and {r["scenario"] for r in calls} == {"critical_cve"}
    assert {"collect", "plan", "worker", "review", "finalize"} <= set(recorder.seen)
    assert all(values == {(RUN_ID, "thread-7", "critical_cve")} for values in recorder.seen.values())


def test_a_scenario_is_refused_in_production(monkeypatch):
    monkeypatch.setattr(run_module, "get_settings", lambda: SimpleNamespace(env="prod"))
    with pytest.raises(ValueError, match="production"):
        run_config(RUN_ID, "thread-1", scenario="clean")
    assert run_config(RUN_ID, "thread-1")["configurable"]["scenario"] is None


# ------------------------------------------------------------------------------------------ checkpoints


async def test_the_state_survives_a_checkpoint(mock_env):
    saver = InMemorySaver(serde=checkpoint_serializer())
    scripted = Scripted(scripts_for(await collect(Mocks())))
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # e.g. "Deserializing unregistered type": the allowlist must cover the state
        result = await supervise(Mocks(), scripted, checkpointer=saver, thread_id="thread-cp")
        config = {"configurable": {"thread_id": "thread-cp"}}
        async with toolbox(Mocks()) as box:
            state = await build_supervisor(box, scripted.models, checkpointer=saver).aget_state(config)
        assert state.values["assessment"] == result.assessment
        namespaces = {c.config["configurable"]["checkpoint_ns"] async for c in saver.alist(config)}
    assert namespaces == {""}  # worker subgraphs keep no checkpoints of their own (no raw tool output stored)


# ----------------------------------------------------------------------------------------------- live


def _real_llm_env() -> dict[str, str]:
    """The developer's real LLM settings, read before the test fixtures replace the environment with fakes."""
    env = {k: v for k, v in os.environ.items() if k.startswith("RELEASE_COPILOT_LLM__")}
    dotenv = Path(__file__).parents[1] / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.startswith("RELEASE_COPILOT_LLM__") and key not in env:
                env[key] = value.strip().strip("\"'")
    return env


REAL_LLM_ENV = _real_llm_env()


@pytest.mark.live
async def test_live_clean_run(mock_env):
    """A real model against the mocks: it must finish within budget with valid answers. Run with ``--live``."""
    if REAL_LLM_ENV.get("RELEASE_COPILOT_LLM__PROVIDER", "openai") == "fake" or not any(k.endswith("_API_KEY") for k in REAL_LLM_ENV):
        pytest.skip("no real LLM configured (RELEASE_COPILOT_LLM__* in the environment or .env)")
    for key, value in REAL_LLM_ENV.items():
        mock_env.setenv(key, value)
    from tests.conftest import _clear_caches

    _clear_caches()
    async with toolbox(Mocks()) as box:
        result = await run_supervisor(build_supervisor(box, default_models), IDENTITY, run_id="run-live", budget=RunBudget(max_tokens=150_000, max_cost_usd=0.50))
    a = result.assessment
    print(result.usage)
    assert [r.task.worker for r in a.reports if r.task.round == 1] == list(WORKERS)
    assert all(r.report.status != "no_answer" for r in a.reports)
    assert a.findings and any(f.verified for f in a.findings)
    assert a.evidence.gaps == ()
