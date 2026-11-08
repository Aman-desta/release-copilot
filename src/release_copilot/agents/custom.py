"""The custom worker graph: an explicit tool loop with budgets, a reserved final answer and injected idempotency keys.

    START -> start -> agent --(tool calls)--> tools --(budget left)--> agent ...
                          \\                        \\
                           `--(no tool calls)-------`--(budget used)--> respond -> report -> END

Compared with the prebuilt agent (``prebuilt.py``), every rule is code we can read and test:
- the loop's budgets are state (``model_calls``, ``tool_calls``), and running out is a state change, not an error;
- the final answer is a separate, **reserved** model call (``with_structured_output``), so a worker that ran out of
  budget still answers with what it found;
- every tool call gets a reply (results, errors and refusals alike), which the model API requires;
- write tools get their idempotency key from the graph (D22).
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from release_copilot.agents.findings import (
    Budget,
    WorkerInput,
    WorkerOutput,
    WorkerReport,
    WorkerResult,
)
from release_copilot.agents.report import build_report
from release_copilot.agents.workers import FINAL_INSTRUCTION, WorkerSpec, task_brief
from release_copilot.evidence.model import ReleaseIdentity
from release_copilot.tools.base import (
    IDEMPOTENCY_ARG,
    idempotency_key,
    is_write_tool,
    tool_error,
)


class CustomWorkerState(TypedDict):
    identity: ReleaseIdentity
    question: str | None
    about: tuple[str, ...]
    messages: Annotated[list[AnyMessage], add_messages]
    model_calls: int
    tool_calls: int
    exhausted: Budget | None
    output: WorkerOutput | None
    report: WorkerReport


# ------------------------------------------------------------------------------------------- YOUR PART


@dataclass(frozen=True)
class ToolBatch:
    messages: list[ToolMessage]  # one per tool call, in the order of the AIMessage's tool_calls
    executed: int  # calls that counted against the budget
    exhausted: bool  # some calls were refused because the budget ran out


async def execute_tool_calls(
    ai: AIMessage,
    tools: Mapping[str, BaseTool],
    *,
    remaining: int,
    run_id: str | None,
    config: RunnableConfig | None = None,
) -> ToolBatch:
    """Run the tool calls in ``ai`` within the remaining tool-call budget (YOUR PART). The node that calls this is
    ``_tools_node`` below; the contract is in tests/test_agents.py (the ``execute_tool_calls`` tests).

    - The first ``remaining`` calls count against the budget and run **concurrently** (``asyncio.TaskGroup``, so a
      bug in one tool cancels the others and raises). Invoke a tool with the whole tool call
      (``await tool.ainvoke(call, config)``, after setting ``call["type"] = "tool_call"``) to get a ``ToolMessage``
      with its artifact.
    - **Every call gets exactly one ToolMessage**, in call order: the model API rejects a conversation where a tool
      call has no reply. Calls the worker can't run get ``status="error"`` and ``tool_error(...)`` content:
      * beyond the budget: ``BudgetExhausted`` (and ``exhausted=True``); these don't count as executed;
      * a tool name the worker doesn't have: ``UnknownTool``; this counts (it used a slot) but runs nothing.
    - **Write tools** (``is_write_tool``) get ``args[IDEMPOTENCY_ARG] = idempotency_key(run_id, call["id"])``,
      replacing anything the model sent. Without a ``run_id`` that's a configuration bug: raise ``ValueError``
      before running any call.
    - Don't modify ``ai`` (it's already in the graph state): copy each call before changing it.
    """
    calls = ai.tool_calls
    allowed = max(0, remaining)

    executable = calls[:allowed]
    refused = calls[allowed:]

    prepared_calls: list[ToolCall] = []
    for call in executable:
        copied_call = call.copy()
        tool = tools.get(copied_call["name"])
        args = copied_call.get("args", {}).copy()

        if tool is not None and is_write_tool(tool):
            if not run_id:
                raise ValueError("run_id is required for write tools")
            call_id = copied_call["id"]
            if call_id is None:
                raise ValueError("tool_call_id is required for write tools")
            args[IDEMPOTENCY_ARG] = idempotency_key(run_id, call_id)

        copied_call["args"] = args
        prepared_calls.append(copied_call)

    async def execute_one(call: ToolCall) -> ToolMessage:
        copied_call = call.copy()
        name = copied_call["name"]
        call_id = copied_call["id"]

        tool = tools.get(call["name"])

        if tool is None:
            return ToolMessage(
                content=tool_error("UnknownTool", f"Tool call refused: unknown tool {name}"),
                name=name,
                tool_call_id=call_id,
                status="error",
            )



        result =  await tool.ainvoke({**call, "type": "tool_call"}, config)
        return result

    async  with asyncio.TaskGroup() as tg:
        tasks = [
            tg.create_task(execute_one(call)) for call in prepared_calls
        ]

    messages = [
        task.result() for task in tasks
    ]

    # Every refused call still needs a response
    for call in refused:
        messages.append(
            ToolMessage(
                content=tool_error("BudgetExhausted", f"Tool call refused due to budget exhaustion: {call['name']}"),
                name=call["name"],
                tool_call_id=call["id"],
                status="error",
            )
        )

    return ToolBatch(
        messages=messages,
        executed=len(executable),
        exhausted=bool(refused),
    )


# ----------------------------------------------------------------------------------------------- graph


def build_custom_worker(spec: WorkerSpec, tools: Sequence[BaseTool], model: BaseChatModel) -> CompiledStateGraph:
    by_name = {t.name: t for t in tools}
    with_tools = model.bind_tools(list(tools))
    answer = model.with_structured_output(WorkerOutput, include_raw=True)

    async def start(state: CustomWorkerState) -> dict:
        return {
            "messages": [SystemMessage(spec.prompt), HumanMessage(task_brief(state))],
            "model_calls": 0,
            "tool_calls": 0,
            "exhausted": None,
            "output": None,
        }

    async def agent(state: CustomWorkerState, config: RunnableConfig) -> dict:
        ai = await with_tools.ainvoke(state["messages"], config)
        calls = state["model_calls"] + 1
        update: dict = {"messages": [ai], "model_calls": calls}
        if ai.tool_calls and calls >= spec.max_model_calls:
            update["exhausted"] = "model_calls"  # its last loop call, and it still wants more evidence
        return update

    async def tools_node(state: CustomWorkerState, config: RunnableConfig) -> dict:
        batch = await execute_tool_calls(
            state["messages"][-1],  # type: ignore[arg-type]  (routed here only after an AIMessage with tool calls)
            by_name,
            remaining=spec.max_tool_calls - state["tool_calls"],
            run_id=config.get("configurable", {}).get("run_id"),
            config=config,
        )
        update: dict = {"messages": batch.messages, "tool_calls": state["tool_calls"] + batch.executed}
        if batch.exhausted:
            update["exhausted"] = "tool_calls"
        return update

    async def respond(state: CustomWorkerState, config: RunnableConfig) -> dict:
        # include_raw=True returns {"raw", "parsed", "parsing_error"}; the declared type is dict | BaseModel.
        result = cast(dict, await answer.ainvoke([*state["messages"], HumanMessage(FINAL_INSTRUCTION)], config))
        return {"output": None if result["parsing_error"] else result["parsed"], "model_calls": state["model_calls"] + 1}

    async def report(state: CustomWorkerState) -> dict:
        return {
            "report": build_report(
                spec, state["messages"], output=state["output"], model_calls=state["model_calls"],
                tool_calls=state["tool_calls"], exhausted=state["exhausted"],
            )
        }

    def after_agent(state: CustomWorkerState) -> Literal["tools", "respond"]:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "respond"

    def after_tools(state: CustomWorkerState) -> Literal["agent", "respond"]:
        return "respond" if state["exhausted"] else "agent"

    graph = StateGraph(CustomWorkerState, input_schema=WorkerInput, output_schema=WorkerResult)
    graph.add_node("start", start)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools_node)
    graph.add_node("respond", respond)
    graph.add_node("report", report)
    graph.add_edge(START, "start")
    graph.add_edge("start", "agent")
    graph.add_conditional_edges("agent", after_agent)
    graph.add_conditional_edges("tools", after_tools)
    graph.add_edge("respond", "report")
    graph.add_edge("report", END)
    # No checkpoints of its own (checkpointer=False): a worker is read-only, so a resumed run simply reruns it, and
    # its raw tool output (which can hold personal data) never reaches the checkpoint store.
    return graph.compile(name=f"{spec.name}_worker", checkpointer=False)

