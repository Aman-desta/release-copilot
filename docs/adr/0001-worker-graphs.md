# ADR 0001: Evidence workers as a custom graph or the prebuilt agent

- **Status:** accepted (A2, 2026-11-03)
- **Versions measured:** langgraph 1.2.19, langchain 1.4.8, langchain-core 1.7.4

## Context

Each evidence worker (A2) is a tool-calling loop: the model calls its tools, reads the results, and finally answers
with structured findings that cite evidence ids. LangGraph offers two ways to build that loop:

1. **Prebuilt:** a ready-made agent. LangGraph's `create_react_agent` is **deprecated since LangGraph 1.0**; its
   successor is LangChain's `create_agent`, which adds *middleware* (hooks around model and tool calls) and
   structured output (`response_format`).
2. **Custom:** our own `StateGraph` with explicit nodes (`agent`, `tools`, `respond`) and routing.

The story asks for both, one worker each, and a comparison. Jira uses the prebuilt agent (`agents/prebuilt.py`);
Change, Quality and Deploy use the custom graph (`agents/custom.py`). Both sit behind the same interface: input
`{identity}`, output `{report: WorkerReport}`, so the supervisor (A3) can't tell them apart, and both build their
report with the same code (`agents/report.py`).

Requirements that decide between them: budgets that end with a gap rather than an exception, a structured answer
even after a budget runs out, idempotency keys injected by the graph (D22), and behaviour we can assert in tests.

## What we measured

Every row is a test in `tests/test_agents.py`, run with the scripted fake model.

| | Custom graph | Prebuilt `create_agent` |
|---|---|---|
| Model calls, normal run with N tool turns | N + 2: tool turns, a turn without tool calls, then the reserved answer | N + 1: the answer is a tool call (`ToolStrategy`) inside the loop |
| Tool budget runs out | Calls over budget get a `BudgetExhausted` reply; the graph goes straight to the answer | `ToolCallLimitMiddleware(exit_behavior="continue")` refuses them; the model is called again and can answer |
| Model budget runs out | Still answers: the final call is reserved outside the loop budget | **Ends without an answer**: the answer shares the budget (`ModelCallLimitMiddleware`) |
| Detecting "budget ran out" | State fields we set (`exhausted`) | Middleware counters, visible only with `stream_mode="values"`; the tool counter **also counts the structured-answer call** (a run that uses exactly its budget reads as 2/1 and gets a spurious "limit exceeded" message), so we count tool calls from the messages instead |
| Idempotency keys for write tools | The tools node injects them (`execute_tool_calls`) | Its tool node doesn't fill `InjectedToolArg` values: it would need a `wrap_tool_call` middleware |
| Every tool call gets a reply | Our code guarantees it, and a test checks it | The library guarantees it |
| Code we own | About 175 lines with `execute_tool_calls`, the loop rules in plain sight | About 85 lines, mostly wiring and working around the counters |

## Decision

- **Custom graph by default** for evidence workers. The deciding properties: a worker that runs out of budget still
  returns findings (a partial answer plus a `BudgetExhausted` gap beats no answer), budgets and refusals are state
  we can read and test, and write tools can get idempotency keys from the graph.
- **The prebuilt agent stays for the Jira worker**: it's read-only and has two tools, so the prebuilt's gaps don't
  bite, and keeping one gives a live comparison (and a fallback if the custom loop has a bug).
- The extra model call of the custom graph is the price of the reserved answer. If token cost matters later (O3),
  binding `WorkerOutput` as a tool *inside* the loop removes it, at the cost of the reserved answer.

## When the prebuilt agent is enough

- Read-only tools, a generous budget, and no need for a partial answer when a budget runs out.
- Standard concerns that middleware already covers: summarization, human-in-the-loop, PII redaction, model fallback,
  tool retry. Writing these as nodes would be reinventing them.
- Prototypes, where the loop's details don't matter yet.

Choose a custom graph when the loop itself carries policy: budgets with defined outcomes, reserved steps,
injected arguments, routing that depends on what a tool returned, or extra steps (validation, reflection) between
model calls.

## Consequences

- Two implementations to maintain, behind one interface and one report builder.
- Upgrading `langchain` can change middleware behaviour (for example the counter quirk above). The tests pin what we
  rely on, so an upgrade that changes it fails loudly.
- A3 can run either kind of worker as a subgraph, in parallel with `Send`, because both return only a `WorkerReport`.

## Interview summary

"The prebuilt agent is a good default: a loop, structured output and middleware for budgets. We kept it for the
simple read-only worker. For the others we wrote the loop ourselves, because the loop carries policy: when a budget
runs out we still want an answer and a recorded gap, not an exception or silence, and write tools need idempotency
keys from the graph rather than the model. We measured the difference with the same scripted model on both."
