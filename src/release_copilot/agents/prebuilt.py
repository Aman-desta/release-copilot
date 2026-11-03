"""The prebuilt worker: LangChain's ``create_agent`` (the successor of LangGraph's deprecated ``create_react_agent``),
with budgets from its middleware, wrapped in a graph with the same input and output as the custom worker.

What the prebuilt agent gives us, and what it costs (docs/adr/0001-worker-graphs.md):
- budgets are middleware: ``ModelCallLimitMiddleware`` and ``ToolCallLimitMiddleware``;
- the final answer is a tool call (``ToolStrategy``) inside the loop, so it shares the model-call budget, and a
  worker that runs out of model calls ends **without an answer** (the custom graph reserves one);
- ``ToolCallLimitMiddleware`` counts the structured-answer call as a tool call, so its counter can't tell us whether
  the budget really ran out: we count the worker's own tool calls from the messages instead;
- its tool node doesn't fill ``InjectedToolArg`` values, so this worker must stay read-only (no idempotency keys).
"""

from collections.abc import Sequence
from typing import TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from release_copilot.agents.findings import Budget, WorkerInput, WorkerOutput, WorkerReport, WorkerResult
from release_copilot.agents.report import build_report, requested_tool_calls
from release_copilot.agents.workers import WorkerSpec, release_brief
from release_copilot.evidence.model import ReleaseIdentity
from release_copilot.tools.base import is_write_tool

# LangGraph steps per model call inside create_agent (model, tools and middleware nodes), with room to spare.
STEPS_PER_MODEL_CALL = 6


class PrebuiltWorkerState(TypedDict):
    identity: ReleaseIdentity
    report: WorkerReport


def build_prebuilt_worker(spec: WorkerSpec, tools: Sequence[BaseTool], model: BaseChatModel) -> CompiledStateGraph:
    if any(is_write_tool(t) for t in tools):
        raise ValueError(f"{spec.name}: the prebuilt worker can't inject idempotency keys, so it can't have write tools")
    answer_calls = spec.max_model_calls + 1  # the loop's calls plus the final answer, as in the custom graph
    agent = create_agent(
        model,
        list(tools),
        system_prompt=spec.prompt,
        middleware=[
            ModelCallLimitMiddleware(run_limit=answer_calls, exit_behavior="end"),
            # "continue": calls over the budget are refused with an error message and the model can still answer.
            ToolCallLimitMiddleware(run_limit=spec.max_tool_calls, exit_behavior="continue"),
        ],
        response_format=ToolStrategy(WorkerOutput),
        name=f"{spec.name}_agent",
    ).with_config(recursion_limit=STEPS_PER_MODEL_CALL * answer_calls)
    names = [t.name for t in tools]

    async def run_agent(state: PrebuiltWorkerState, config: RunnableConfig) -> dict:
        final: dict = {}
        # stream_mode="values" ends with the full state, including the middleware's counters (invoke() drops them).
        async for final in agent.astream({"messages": [HumanMessage(release_brief(state["identity"]))]}, config, stream_mode="values"):
            pass
        messages = final["messages"]
        output = final.get("structured_response")
        model_calls = final.get("run_model_call_count", 0)
        requested = requested_tool_calls(messages, names)
        exhausted: Budget | None = None
        if requested > spec.max_tool_calls:
            exhausted = "tool_calls"
        elif output is None and model_calls >= answer_calls:
            exhausted = "model_calls"
        report = build_report(
            spec, messages, output=output, model_calls=model_calls,
            tool_calls=min(requested, spec.max_tool_calls), exhausted=exhausted,
        )
        return {"report": report}

    graph = StateGraph(PrebuiltWorkerState, input_schema=WorkerInput, output_schema=WorkerResult)
    graph.add_node("agent", run_agent)
    graph.add_edge(START, "agent")
    graph.add_edge("agent", END)
    return graph.compile(name=f"{spec.name}_worker")

