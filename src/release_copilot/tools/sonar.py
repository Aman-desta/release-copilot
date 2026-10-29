"""SonarQube read tool: coverage and quality gate for a branch."""

import asyncio
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool
from pydantic import Field

from release_copilot.tools.base import Evidence, ToolArgs, ToolPayload, View, make_tool
from release_copilot.tools.github import Ref

if TYPE_CHECKING:
    from release_copilot.tools.toolbox import Toolbox

SHOWN_METRICS = ("coverage", "new_coverage", "bugs", "vulnerabilities", "security_hotspots", "code_smells", "tests", "test_failures")


class QualityArgs(ToolArgs):
    branch: Ref = Field(description="Branch to analyse, e.g. release/2.4.0.")
    compare_to: Ref | None = Field(default=None, description="Optional earlier branch to compare with, e.g. release/2.3.1.")


class ConditionView(View):
    metric: str
    actual: str | None = None
    rule: str  # e.g. "< 80 fails"


class QualityView(View):
    gate_id: str
    gate: str  # OK | ERROR
    failing_conditions: list[ConditionView] = []
    measures_id: str
    measures: dict[str, float]
    change_vs_previous: dict[str, float] | None = None  # metric -> difference (branch minus compare_to)


async def _quality(box: "Toolbox", args: QualityArgs) -> ToolPayload:
    gate, measures = await asyncio.gather(box.sonar.quality_gate(args.branch), box.sonar.measures(args.branch))
    previous = await box.sonar.measures(args.compare_to) if args.compare_to else None
    shown = {k: v for k, v in measures.values.items() if k in SHOWN_METRICS}
    delta = None
    if previous is not None:
        delta = {k: round(v - previous.values[k], 2) for k, v in shown.items() if k in previous.values}
    view = QualityView(
        gate_id=gate.evidence_id,
        gate=gate.status,
        failing_conditions=[
            ConditionView(metric=c.metric, actual=c.actual, rule=f"{'<' if c.comparator == 'LT' else '>'} {c.threshold} fails")
            for c in gate.failing
        ],
        measures_id=measures.evidence_id,
        measures=shown,
        change_vs_previous=delta,
    )
    objects: list[object] = [gate, measures, *([previous] if previous else [])]
    evidence = [Evidence(gate.evidence_id), Evidence(measures.evidence_id), *([Evidence(previous.evidence_id)] if previous else [])]
    return ToolPayload(view=view, evidence=evidence, objects=objects)


def build(box: "Toolbox") -> list[BaseTool]:
    return [
        make_tool(
            lambda a: _quality(box, a),
            name="sonar_quality",
            description=(
                "Read code quality for a branch: the quality gate (and which conditions fail), test coverage overall "
                "and on new code, bugs and vulnerabilities. Pass compare_to to see the change since the last release."
            ),
            args_schema=QualityArgs, access="read", domain="sonar",
        )
    ]
