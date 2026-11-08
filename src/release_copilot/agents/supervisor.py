"""The supervisor: collect the facts, fan out to the workers in parallel, ask follow-ups, and assess the result.

    START -> collect -> plan --Send x N--> worker (parallel) -> review --Send--> worker (follow-ups) -> review
                           \\                                       \\
                            `--(no workers)--> review               `--> finalize -> END

- **collect**: the deterministic evidence (A1). Facts come from code, so the record the document cites, and the
  policy reads, doesn't depend on what a model chose to look at.
- **plan**: which workers run, decided in code: a worker whose every evidence section is unknown (its sources are
  down) is skipped with a gap instead of spending its budget on errors.
- **worker**: one A2 worker subgraph per ``Send``, in parallel. Each returns only a report; the ``reports`` reducer
  merges the parallel results. Its messages and raw tool output stay private (context isolation).
- **review**: after a round, a model may propose follow-ups (judgment); code checks them (``accept_follow_ups``)
  and hands the accepted ones to workers with ``Command(goto=[Send(...)], update=...)``. One follow-up round at most.
- **finalize**: the ``ReleaseAssessment``: evidence, findings with verified citations, and coverage by the policy.

Budgets: each worker has its own (A2); the run has a global token and cost budget (``budget.BudgetGuard``), and a
``recursion_limit`` per graph. Running out is a gap, never a crash.
"""

import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict, cast, get_args, get_origin

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Send
from pydantic import BaseModel, ConfigDict

from release_copilot.agents.budget import BudgetExceeded, BudgetGuard, RunBudget
from release_copilot.agents.factory import build_worker
from release_copilot.agents.findings import (
    Finding,
    Severity,
    WorkerReport,
    WorkerStatus,
)
from release_copilot.agents.followups import (
    MAX_FOLLOW_UPS,
    SUPERVISOR_PROMPT,
    FollowUpPlan,
    RejectedFollowUp,
    WorkerTask,
    accept_follow_ups,
    follow_up_brief,
    needs_follow_up,
)
from release_copilot.agents.policy import (
    DEFAULT_POLICY,
    Coverage,
    EvidencePolicy,
    assess_coverage,
    section_value,
)
from release_copilot.agents.run import (
    RunInfo,
    release_thread_id,
    run_config,
    run_context,
    run_info,
)
from release_copilot.agents.workers import WORKERS, WorkerSpec
from release_copilot.evidence.collect import collect_release_evidence
from release_copilot.evidence.index import EvidenceIndex
from release_copilot.evidence.model import EvidenceGap, ReleaseEvidence, ReleaseIdentity
from release_copilot.llm.factory import get_chat_model
from release_copilot.llm.usage import UsageTracker
from release_copilot.tools.toolbox import Toolbox

MAX_ROUNDS = 2  # the workers' own round, then at most one follow-up round
DEFAULT_BUDGET = RunBudget()
SEVERITY_RANK: dict[Severity, int] = {s: i for i, s in enumerate(get_args(Severity))}  # info=0 ... critical=4

# Which model each part of the run uses, by name: "supervisor", or a worker's name. Tests give each worker its own
# scripted model, so parallel workers don't race for one script.
Models = Callable[[str], BaseChatModel]


def default_models(name: str) -> BaseChatModel:
    return get_chat_model("supervisor" if name == "supervisor" else "worker")


# ---------------------------------------------------------------------------------------------- results


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TaskReport(_Frozen):
    """A worker's report for one task, as the supervisor keeps it: evidence ids instead of raw tool artifacts.

    The artifacts hold every object the tools returned (personal data included) and don't survive a checkpoint
    round trip as objects, so the supervisor's state keeps only the ids, which is all it needs: the facts are in
    ``ReleaseEvidence``, and the ids are what citations and follow-ups are checked against.
    """

    task: WorkerTask
    report: WorkerReport
    collected: tuple[str, ...] = ()  # the evidence ids the worker's tools returned

    @classmethod
    def of(cls, task: WorkerTask, report: WorkerReport) -> "TaskReport":
        return cls(task=task, report=report.model_copy(update={"artifacts": ()}), collected=report.index().ids)


class AttributedFinding(_Frozen):
    worker: str
    round: int
    finding: Finding
    verified: bool  # every cited id is evidence this run collected; A4 refuses to cite an unverified finding


class ReleaseAssessment(_Frozen):
    """The supervisor's result: what A4 writes the document from, and what A5 decides on."""

    run: RunInfo
    identity: ReleaseIdentity
    evidence: ReleaseEvidence
    coverage: Coverage
    findings: tuple[AttributedFinding, ...]  # most severe first
    reports: tuple[TaskReport, ...]  # by round, then in worker order
    follow_ups: tuple[WorkerTask, ...] = ()
    rejected_follow_ups: tuple[RejectedFollowUp, ...] = ()
    gaps: tuple[EvidenceGap, ...] = ()  # the supervisor's own: skipped workers, spent budget, an invalid answer

    def all_gaps(self) -> tuple[EvidenceGap, ...]:
        return (*self.evidence.gaps, *self.gaps, *(g for r in self.reports for g in r.report.gaps))


# ------------------------------------------------------------------------------------------------ state


class SupervisorInput(TypedDict):
    identity: ReleaseIdentity


class SupervisorOutput(TypedDict):
    assessment: ReleaseAssessment


class SupervisorState(TypedDict):
    identity: ReleaseIdentity
    evidence: ReleaseEvidence
    skipped: list[str]
    round: int
    # Reducers: parallel workers write in the same step, so these channels merge their updates (operator.add
    # concatenates) instead of rejecting them. A channel without a reducer accepts one write per step.
    reports: Annotated[list[TaskReport], operator.add]
    gaps: Annotated[list[EvidenceGap], operator.add]
    follow_ups: Annotated[list[WorkerTask], operator.add]
    rejected: Annotated[list[RejectedFollowUp], operator.add]
    assessment: ReleaseAssessment


class WorkerJob(TypedDict):
    """What a ``Send`` hands the worker node: not the supervisor's state, only what that worker needs."""

    identity: ReleaseIdentity
    task: WorkerTask


# ------------------------------------------------------------------------------------------------ graph


def plan_workers(evidence: ReleaseEvidence, specs: Mapping[str, WorkerSpec]) -> tuple[list[WorkerTask], list[str]]:
    """The first round: every worker, except one whose evidence sections are all unknown (its sources are down)."""
    tasks, skipped = [], []
    for name, spec in specs.items():
        if spec.reads and all(section_value(evidence, s) is None for s in spec.reads):
            skipped.append(name)
        else:
            tasks.append(WorkerTask(worker=name))
    return tasks, skipped


def worker_recursion_limit(spec: WorkerSpec) -> int:
    """A worker graph's steps: start, an agent and a tools step per model call, the answer and the report."""
    return 2 * spec.max_model_calls + 5


def build_supervisor(
    box: Toolbox,
    models: Models = default_models,
    *,
    specs: Mapping[str, WorkerSpec] = WORKERS,
    policy: EvidencePolicy = DEFAULT_POLICY,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    planner = models("supervisor").with_structured_output(FollowUpPlan, include_raw=True)

    def known_ids(state: SupervisorState) -> set[str]:
        return set(EvidenceIndex.from_evidence(state["evidence"]).ids) | {i for r in state["reports"] for i in r.collected}

    def job(state: SupervisorState, task: WorkerTask) -> Send:
        return Send("worker", WorkerJob(identity=state["identity"], task=task))

    async def collect(state: SupervisorState, config: RunnableConfig) -> dict:
        with run_context(config):
            evidence = await collect_release_evidence(box, state["identity"])
        return {"evidence": evidence, "round": 1}

    async def plan(state: SupervisorState, config: RunnableConfig) -> Command[Literal["worker", "review"]]:
        tasks, skipped = plan_workers(state["evidence"], specs)
        gaps = [_skipped_gap(specs[name], state["evidence"]) for name in skipped]
        update = {"skipped": skipped, "gaps": gaps}
        if not tasks:
            return Command(goto="review", update=update)
        return Command(goto=[job(state, t) for t in tasks], update=update)

    async def worker(job: WorkerJob, config: RunnableConfig) -> dict:
        task = job["task"]
        spec = specs[task.worker]
        graph = build_worker(spec, box, models(task.worker))
        child: RunnableConfig = {**config, "recursion_limit": worker_recursion_limit(spec)}
        with run_context(config):
            try:
                out = await graph.ainvoke({"identity": job["identity"], "question": task.question, "about": task.about}, child)
                report: WorkerReport = out["report"]
            except BudgetExceeded as exc:  # the run's budget, not the worker's: its partial work is lost
                report = WorkerReport(
                    worker=spec.name, status="exhausted", model_calls=0, tool_calls=0,
                    gaps=(_agent_gap(spec.name, "exhausted", "BudgetExhausted", str(exc)),),
                )
        return {"reports": [TaskReport.of(task, report)]}

    async def review(state: SupervisorState, config: RunnableConfig) -> Command[Literal["worker", "finalize"]]:
        current = state["round"]
        latest = [r.report for r in state["reports"] if r.task.round == current]
        if current >= MAX_ROUNDS or not needs_follow_up(latest):
            return Command(goto="finalize")

        ran = [specs[name] for name in specs if name not in state["skipped"]]
        messages = [
            SystemMessage(SUPERVISOR_PROMPT.format(limit=MAX_FOLLOW_UPS)),
            HumanMessage(follow_up_brief(state["identity"], latest, ran)),
        ]
        with run_context(config):
            try:
                result = cast(dict, await planner.ainvoke(messages, config))  # include_raw=True: {"raw", "parsed", "parsing_error"}
            except BudgetExceeded as exc:
                return Command(goto="finalize", update={"gaps": [_agent_gap("supervisor.follow_ups", "exhausted", "BudgetExhausted", str(exc))]})
        plan: FollowUpPlan | None = result["parsed"]  # None also when the model answered in text, without the tool call
        if plan is None or result["parsing_error"] is not None:
            gap = _agent_gap("supervisor.follow_ups", "unavailable", "InvalidOutput", "the supervisor's follow-up answer was invalid, so no follow-ups ran")
            return Command(goto="finalize", update={"gaps": [gap]})

        decision = accept_follow_ups(plan, known_ids=known_ids(state), available=[s.name for s in ran], round=current + 1)
        update: dict[str, Any] = {"follow_ups": list(decision.accepted), "rejected": list(decision.rejected)}
        if not decision.accepted:
            return Command(goto="finalize", update=update)
        return Command(goto=[job(state, t) for t in decision.accepted], update={**update, "round": current + 1})

    async def finalize(state: SupervisorState, config: RunnableConfig) -> dict:
        order = {name: i for i, name in enumerate(specs)}
        reports = sorted(state["reports"], key=lambda r: (r.task.round, order[r.task.worker]))
        known = known_ids(state)
        findings = [
            AttributedFinding(worker=r.task.worker, round=r.task.round, finding=f, verified=all(i in known for i in f.evidence_ids))
            for r in reports
            for f in r.report.findings
        ]
        findings.sort(key=lambda a: SEVERITY_RANK[a.finding.severity], reverse=True)  # stable: ties keep report order
        statuses: dict[str, WorkerStatus | None] = {name: None for name in specs}
        statuses |= {r.task.worker: r.report.status for r in reports if r.task.round == 1}
        assessment = ReleaseAssessment(
            run=run_info(config),
            identity=state["identity"],
            evidence=state["evidence"],
            coverage=assess_coverage(state["evidence"], statuses, policy),
            findings=tuple(findings),
            reports=tuple(reports),
            follow_ups=tuple(state.get("follow_ups", ())),
            rejected_follow_ups=tuple(state.get("rejected", ())),
            gaps=tuple(state.get("gaps", ())),
        )
        return {"assessment": assessment}

    graph = StateGraph(SupervisorState, input_schema=SupervisorInput, output_schema=SupervisorOutput)
    graph.add_node("collect", collect)
    graph.add_node("plan", plan, destinations=("worker", "review"))
    # Its input is a Send's payload, not the supervisor's state; add_node's types only know the state.
    graph.add_node("worker", worker, input_schema=WorkerJob)  # type: ignore[call-overload]
    graph.add_node("review", review, destinations=("worker", "finalize"))
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "plan")
    graph.add_edge("worker", "review")  # fan-in: review runs once, after every worker of the step has finished
    graph.add_edge("finalize", END)
    return graph.compile(name="supervisor", checkpointer=checkpointer)


@dataclass(frozen=True)
class SupervisorRun:
    assessment: ReleaseAssessment
    usage: dict[str, Any]  # UsageTracker.summary(): tokens and cost by role and model


async def run_supervisor(
    supervisor: CompiledStateGraph,
    identity: ReleaseIdentity,
    *,
    run_id: str,
    thread_id: str | None = None,
    scenario: str | None = None,
    budget: RunBudget = DEFAULT_BUDGET,
    callbacks: list[Any] | None = None,
) -> SupervisorRun:
    tracker = UsageTracker()
    config = run_config(
        run_id,
        thread_id or release_thread_id(identity, run_id),
        scenario=scenario,
        callbacks=[tracker, BudgetGuard(budget, tracker), *(callbacks or [])],
    )
    out = await supervisor.ainvoke(SupervisorInput(identity=identity), config)
    return SupervisorRun(assessment=out["assessment"], usage=tracker.summary())


# ---------------------------------------------------------------------------------------------- helpers


def _agent_gap(section: str, kind: Literal["exhausted", "unavailable", "blocked"], error_type: str, message: str) -> EvidenceGap:
    return EvidenceGap(source="agent", section=section, kind=kind, error_type=error_type, message=message)


def _skipped_gap(spec: WorkerSpec, evidence: ReleaseEvidence) -> EvidenceGap:
    causes = [g for s in spec.reads for g in evidence.gaps_for(s)]
    return EvidenceGap(
        source="agent", section=spec.name, kind="blocked", error_type="Blocked",
        message=f"the {spec.name} worker didn't run: every evidence section it reads is unavailable ({', '.join(spec.reads)})",
        retryable=any(g.retryable for g in causes),
    )


def checkpoint_serializer() -> JsonPlusSerializer:
    """A serializer that restores the supervisor's state types from a checkpoint, and nothing else (strict).

    LangGraph's default restores any class a checkpoint names (with a warning), which a future version blocks; an
    allowlist is the safe form. It is built from the state's types, so a new model in the state is covered.
    """
    roots = (ReleaseIdentity, ReleaseEvidence, TaskReport, EvidenceGap, WorkerTask, RejectedFollowUp, ReleaseAssessment)
    return JsonPlusSerializer(allowed_msgpack_modules=sorted((t.__module__, t.__name__) for t in _model_types(roots)))


def _model_types(roots: tuple[type[BaseModel], ...]) -> set[type[BaseModel]]:
    found: set[type[BaseModel]] = set()

    def visit(annotation: Any) -> None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation in found:
                return
            found.add(annotation)
            for field in annotation.model_fields.values():
                visit(field.annotation)
        for arg in get_args(annotation) if get_origin(annotation) is not None else ():
            visit(arg)

    for root in roots:
        visit(root)
    return found
