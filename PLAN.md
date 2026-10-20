# Release Copilot: Implementation Plan

A learning project for AI-engineer interviews. It implements this resume bullet end to end:

> Built a multi-agent LangGraph platform (Python) using supervisor/worker agents that pulled Jira tickets,
> Azure App Service deployment state, test coverage, and security-scan results to automate release-document
> generation, change-request creation, approval routing, and rollback planning; instrumented agent runs with
> tracing and LLM-as-a-Judge evaluation for relevance and faithfulness.

It also covers the other agent skills on the resume (MCP, structured outputs, memory, guardrails, RAG,
OpenTelemetry, Langfuse/Phoenix), plus a few tools that are in high demand but missing from the resume.

**How we work:** Claude built Phase 0. From Phase 1 on we use coach mode: concept, then spec, then code, then
review. Claude writes the scaffolding and the boring parts; the user writes the core agent logic.

## The scenario

When a release of `payments-api` (a .NET service on Azure App Service, org `acme-fin`) is cut, the
copilot does the following:

1. Collects evidence: Jira issues in the fix version, the GitHub diff and PRs, check runs, Dependabot and
   code-scanning alerts, SonarQube coverage and quality gate, and Azure deployment state and metrics.
2. Writes a release document in which every claim cites its evidence (ticket keys, SHAs, alert ids).
3. Scores risk, creates a ServiceNow change request (without duplicates), and routes approval by risk tier.
4. Pauses for human approval (durable), then swaps the slot, watches health, and rolls back if production degrades.

Each run is traced and scored by an LLM judge against a golden dataset built from the mock scenarios.

## Phases

| # | Phase | Builds | Interview topics |
|---|---|---|---|
| 0 | **Mock enterprise services** ✅ | Mocks shaped like the real Jira v3, GitHub REST, Azure ARM + Entra token, SonarQube, ServiceNow Table API, Teams/Slack webhooks. Scenarios, fault injection, auth, request log | API contracts, test doubles, scenario-driven testing |
| 1 | **Typed tools + resilient clients** | httpx clients (OAuth2 client credentials with token refresh, pagination via Link headers and nextPageToken, Retry-After), Pydantic models, ADF→text, `@tool` wrappers with structured outputs | Tool calling, structured outputs (JSON Schema), retries/backoff, error taxonomy |
| 2 | **MCP servers (official SDK)** | One MCP server per domain built with `mcp` FastMCP over streamable HTTP; agents load the tools through `langchain-mcp-adapters` | MCP tools, resources and prompts; transports; MCP vs direct API calls |
| 3 | **Worker agents** | Jira, Change (GitHub), Deploy (Azure) and Quality (Sonar + alerts) workers, each a ReAct subgraph with its own state; prebuilt `create_react_agent` vs a custom graph | Tool scoping per agent, context isolation, subgraphs |
| 4 | **Supervisor** | Supervisor that routes with structured output plus `Command(goto=…)` handoffs; parallel evidence collection with `Send`; compared with `langgraph-supervisor`; recursion and budget limits | Supervisor/worker vs swarm vs plan-and-execute; handoffs; shared vs private state |
| 5 | **Release document** | Writer agent fills a template; each claim cites evidence ids; optional policy and template RAG via source-bound | Grounding, citations, context engineering |
| 6 | **Change request, approval, rollback** | Risk scoring (deterministic rules plus LLM rationale), idempotent CR creation (`correlation_id`), risk-tiered approvers, `interrupt()` with a **Postgres checkpointer**, resume from the ServiceNow approval webhook, slot swap, health watch, rollback planner with reflection | HITL, durable execution, idempotency for side effects, time travel |
| 7 | **Memory** | Short-term memory (checkpointer); long-term memory in the LangGraph `Store` with semantic search (lessons from past releases, per-service quirks); message trimming and summarization | Agent memory types, context-window management |
| 8 | **Guardrails** | PII redaction on tool output, prompt-injection detection on untrusted tool content, output groundedness check, deterministic policy gate (a critical CVE can never be auto-approved); NeMo Guardrails compared with custom checks | Defense in depth, indirect prompt injection, OWASP LLM Top 10 |
| 9 | **Observability + evaluation** | OpenTelemetry spans sent to **Langfuse** and **Arize Phoenix** (LangSmith optional via env); token and cost accounting; LLM-as-a-Judge relevance and faithfulness with rubrics, calibrated on hand labels; golden dataset from the scenarios; **DeepEval** in pytest as a CI regression gate; trajectory evaluation (did it call the right tools?) | LLMOps, offline vs online evals, judge bias, regression gates |
| 10 | **Ship it** | FastAPI service with SSE streaming; webhook trigger (pipeline finished → queue → run); Docker Compose (mocks, Postgres, Langfuse, Phoenix); GitHub Actions running tests and evals | Deploying agents, async runs, event-driven triggers |
| 11 | **Same workflow, other frameworks** (stretch) | Port one worker and the approval step to the **OpenAI Agents SDK** and **Microsoft Agent Framework / Semantic Kernel (.NET)** | Framework trade-offs; the .NET + Azure AI story |

### Additions (high demand, missing from the resume)
- **LiteLLM / Azure OpenAI**: model gateway and provider switching (Azure OpenAI is the default in banks), plus fallbacks and cost tracking. Added in Phase 1 as the model factory.
- **DeepEval**: the pytest-native eval framework that shows up most in job descriptions. Phase 9.
- **OpenAI Agents SDK** and **Microsoft Agent Framework**: the two frameworks most often listed next to LangGraph. Phase 11.
- **Trajectory evals** and **durable execution**: what senior interviews probe. Phases 6 and 9.

### Cut (low value for the job search)
- A2A protocol, LlamaIndex Workflows, CrewAI/AutoGen: mention them in interviews, but don't build them.
- LangGraph Platform (managed deployment): Phase 10 self-hosts instead.

## Phase 0 reference: mock services

One FastAPI app (`python -m release_copilot.mocks`, port 9100). Each service lives under its own prefix
and mirrors the real paths, so a real client only needs its base URL changed.

| Service | Base URL | Auth |
|---|---|---|
| Jira Cloud REST v3 | `http://localhost:9100/jira` | Basic `email:api_token` |
| GitHub REST (+ Dependabot, code scanning) | `http://localhost:9100/github` | `Bearer <PAT>` |
| Entra ID token | `http://localhost:9100/azure/login/{tenant}/oauth2/v2.0/token` | client credentials |
| Azure ARM (App Service, Monitor metrics) | `http://localhost:9100/azure/management` | `Bearer <token>`; `api-version` required |
| SonarQube | `http://localhost:9100/sonar` | `Bearer <token>` |
| ServiceNow Table API | `http://localhost:9100/servicenow` | Basic |
| Teams / Slack webhooks | `http://localhost:9100/notify/{teams,slack}/{channel}` | none (secret URL) |
| Control plane | `http://localhost:9100/_admin` | none |

**Scenarios** (choose with the `X-Mock-Scenario` header, or set the default with `PUT /_admin/scenario`):
`clean`, `critical_cve`, `coverage_drop`, `failed_deploy`, `prompt_injection`, `pii_in_ticket`.
Each scenario keeps its own mutable state, so evaluation runs for different scenarios can run in parallel.

**Faults:** `POST /_admin/faults` with `{path_prefix, kind: status|latency|timeout, status, delay_ms, times, retry_after}`
(for example, "the first 2 Jira calls return 429"). Single-request override: header `X-Mock-Fault: 503`.

**Inspection:** `GET /_admin/requests` (what the agent called), `GET /_admin/notifications`,
`POST /_admin/servicenow/approvals/{sys_id}` (act as the human approver; fires the registered callback).
