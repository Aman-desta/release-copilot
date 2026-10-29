# Release Copilot: Stories

Stories are written as production requirements, not learning steps. We pick one at a time; each one is
finished when its acceptance criteria pass in `uv run pytest` (no real LLM or network calls in tests) and it
has been reviewed.

**Priority:** P0 = the core loop needs it · P1 = production grade · P2 = stretch
**Size:** S ≈ half a day · M ≈ 1–2 days · L ≈ 3–4 days

## Status convention

Each story's status goes at the end of its `###` heading line: *(none)* = open · 🔵 In progress ·
🟣 Awaiting review · ✅ Done.

## Status at a glance

| Status | Stories |
|---|---|
| Done | F1, F2, F3, T1, T2, T3, T4 |
| Next up | T5 → A1 |

**Suggested order:** F2, F3, T1, T2, T3, T4, T5 → A1, A2, A3 (first end-to-end run) → O1 (trace everything from here on) →
A4, A5, P1, A6, A7, A8 → P2, P3 → G1, G2 → O2, O3, O4 → M1, M2 → S1, S2, S3 → X1, X2.

## Architecture decisions

| Topic | Decision | Why |
|---|---|---|
| Database | **Postgres 17 + pgvector**, one container (`pgvector/pgvector:pg17`) | Serves the LangGraph checkpointer, the long-term memory store, the app tables and the policy vectors. The standard production LangGraph setup |
| Migrations | Alembic for app tables; LangGraph's `.setup()` for its own tables | Versioned schema, kept separate from library-owned tables |
| Models | OpenAI by default; Azure OpenAI is a configuration switch; selected per role (supervisor, worker, writer, judge) | Banks use Azure OpenAI; the judge must not be the generator's model |
| Tracing | OpenTelemetry → Arize Phoenix + Langfuse; LangSmith opt-in via env | Vendor-neutral instrumentation, one switch per backend |
| Queue | Azure Service Bus **emulator** (official container) | Same trigger pattern as the resume's Service Bus work |
| Integrations | Typed clients (T*) first; MCP servers (M*) wrap the same clients | Agents can switch between direct calls and MCP through configuration |

---

## Epic F: Platform foundations

### F1 [P0, M] Mock enterprise services — ✅ Done
Jira, GitHub, Azure (Entra + ARM + Monitor), SonarQube, ServiceNow and Teams/Slack mocks, shaped like the real
APIs, with scenarios, fault injection, a request log and an approval callback. They live in the separate repo
`../release-copilot-mocks`; this repo uses them as an editable dev dependency (the `mocks_http` fixture, in-process).

### F2 [P0, S] Local infrastructure: Postgres + pgvector via Docker Compose — ✅ Done
**As** a developer **I want** one command to start the stateful infrastructure **so that** agents can persist
state and memory locally the same way they will in production.
- `compose.yaml` with `postgres` (`pgvector/pgvector:pg17`, named volume, healthcheck, init script that runs
  `CREATE EXTENSION vector`) and the `mocks` service (built from a `Dockerfile` added to `../release-copilot-mocks`, healthcheck on `/`).
- Separate roles: an `app` role for the application, with no superuser rights. Credentials come only from env (`.env.example` updated).
- Central settings module `release_copilot/config.py` (pydantic-settings, prefix `RELEASE_COPILOT_`), with
  `database_url` and the mock base URLs and credentials. Secrets are `SecretStr` and never logged.
- Alembic set up with a first empty revision; `make` (or `uv run` scripts) targets: `up`, `down`, `migrate`, `psql`.
- Test helper: a pytest fixture that uses `RELEASE_COPILOT_TEST_DATABASE_URL` if set and otherwise skips
  database tests (testcontainers is optional).

**Acceptance**
- `docker compose up -d` makes both services healthy; `alembic upgrade head` succeeds; `SELECT extname FROM pg_extension` includes `vector`.
- The app connects as the `app` role, not as `postgres`.
- Removing the containers and starting again keeps the data (named volume).

### F3 [P0, M] Model gateway: per-role model factory — ✅ Done
**As** the platform **I need** one place that creates chat and embedding models **so that** providers, models,
timeouts and costs are configured instead of hard-coded.
- `release_copilot/llm.py`: `get_chat_model(role)` for roles `supervisor | worker | writer | judge`, and `get_embeddings()`.
  Provider is `openai | azure_openai` (Azure: endpoint, deployment, api-version), via `init_chat_model` / LangChain provider classes.
  LiteLLM proxy is an optional provider (`litellm`), for central routing.
- Timeouts, `max_retries` and a fallback model per role (`.with_fallbacks`). Temperature defaults by role (judge = 0).
- Token and cost accounting: a callback that adds usage by role and model to the run's metadata.
- A deterministic fake model (`FakeMessagesListChatModel` or similar), selectable in tests and with `RELEASE_COPILOT_LLM_PROVIDER=fake`.
- Models are created lazily; nothing calls the network at import time.

**Acceptance**
- Unit tests: role → model mapping, Azure configuration passes through correctly, the fallback fires when the primary raises, usage adds up across calls.
- The judge role refuses to use the same model as the writer unless explicitly overridden (a config validation error).

### F4 [P1, S] Code quality gates
- `ruff` (lint and format) and `mypy --strict` on `src/release_copilot`, pre-commit hooks, a GitHub Actions workflow running lint, types and tests on PRs.

**Acceptance:** CI is green on main; a PR with a type error fails.

---

## Epic T: Integrations and tools

### T1 [P0, M] Resilient HTTP client core — ✅ Done
**As** every integration **I need** a shared async HTTP layer **so that** retries, auth and errors behave the same everywhere.
- `release_copilot/integrations/http.py`: wrapper around `httpx.AsyncClient`, configured per service (base URL, timeouts, auth).
- Retries with exponential backoff and full jitter on connect errors, timeouts, 429, 502, 503 and 504; **honours `Retry-After`**; max attempts
  and a total deadline. **No retry for non-idempotent methods** (POST) unless the caller opts in.
- A simple circuit breaker per service (opens after N consecutive failures, half-opens after a cool-down).
- Auth providers: `BasicAuth`, `BearerToken`, `OAuth2ClientCredentials` (token cache, refresh before expiry,
  a single refresh-and-retry on 401 `ExpiredAuthenticationToken`, concurrent callers share one refresh).
- Error types: `IntegrationError` → `AuthError`, `NotFoundError`, `ValidationError`, `RateLimitedError`,
  `UpstreamUnavailableError`; each carries the service, status and a vendor message pulled from the body.
- Structured logging (one line per attempt: service, method, path template, status, attempt, ms), with a correlation id taken from a contextvar.

**Acceptance (against the mocks with faults)**
- `times=2` 429 faults with `Retry-After: 1` → success on the 3rd attempt, after waiting at least 1s each time.
- A POST that hits a 503 is not retried by default.
- Azure token TTL of 2s → calls after expiry refresh transparently; 10 concurrent calls trigger exactly 1 token request.
- 5 consecutive 503s open the breaker; the next call fails fast without HTTP.

### T2 [P0, M] Jira and GitHub evidence clients — ✅ Done
- `JiraClient`: `search(jql)` (async iterator over `nextPageToken` pages), `get_issue`, `add_comment(markdown)`
  (converts to ADF), `transition`. **ADF → Markdown converter** (paragraphs, lists, code, links, mentions; unknown nodes become plain text).
- `GitHubClient`: `compare(base, head)`, `pulls_for_commit`, `reviews`, `check_runs`, `dependabot_alerts`,
  `code_scanning_alerts`, `create_release`; `Link`-header pagination as an async iterator.
- Pydantic domain models (`Issue`, `Commit`, `PullRequest`, `Review`, `CheckRun`, `DependencyAlert`,
  `CodeScanningAlert`) that pull only the fields agents need; vendor-specific JSON stays inside the client.

**Acceptance:** tests against the mocks cover pagination across pages, ADF round-trips for the seeded
descriptions, and finding the `coverage_drop` hotfix commit as having no PR.

### T3 [P0, S] Azure App Service and Monitor client — ✅ Done
- `AzureAppServiceClient`: `get_site`, `list_slots`, `deployments(slot)`, `metrics(slot, names, timespan, interval)` →
  a typed `MetricSeries`, `swap(source, target)` that **polls `Azure-AsyncOperation` until it finishes**, with a timeout.
- `api-version` is pinned in one constant; the resource ID is built from config (subscription, RG, site).

**Acceptance:** in `failed_deploy`, swap → metrics show a 5xx rate above 10% in production; swap back → it recovers.

### T4 [P0, S] SonarQube, ServiceNow and notification clients — ✅ Done
- `SonarClient`: measures (handles both `value` and `period.value`), quality gate with conditions.
- `ServiceNowClient`: a small query builder (`Q().eq().like().order_by_desc()` → `sysparm_query`), reads
  references (`{link, value}`), `create_change`, `update_change`, `request_approval`, typed `ChangeRequest`.
  Vendor errors (403 business rules) map to a `PolicyViolationError`.
- `Notifier` with Teams (Adaptive Card) and Slack (Block Kit) implementations, behind a common interface.

**Acceptance:** creating a change and then trying `approval=approved` raises `PolicyViolationError`; the quality gate in `coverage_drop` is parsed as failed, along with the failing condition.

### T5 [P0, M] Agent tool layer — 🔵 In progress
**As** an agent **I need** tools with tight schemas and compact outputs **so that** tool use is reliable and cheap.
- LangChain `@tool`s (or `StructuredTool`) over T2–T4 with Pydantic argument schemas, docstrings written for the model, and enums where possible.
- Outputs are **compact, LLM-friendly Pydantic models**, not raw vendor JSON; long text is truncated with a
  "truncated" marker; there is a per-tool output token budget.
- Every fact a tool returns has a stable **evidence ID** (`jira:PAY-102`, `gh:commit:abc1234`,
  `gh:dependabot:34`, `sonar:gate:release/2.4.0`, `azure:deploy:staging:dep-2.4.0-rc.1`) and a source URL.
- Tools are tagged `read` or `write`; write tools need an `idempotency_key` argument and are never given to evidence workers.
- Errors become structured tool results (`{error_type, message, retryable}`), so the model can respond to them.

**Acceptance:** schema snapshot tests; the tool output for the 8-ticket fix version fits within 2k tokens; a
mocked 404 comes back as a structured error, not an exception.

---

## Epic A: Agents and workflow

### A1 [P0, S] Release evidence model
- `ReleaseEvidence` (Pydantic): release identity, tickets, changes, PRs and reviews, checks, alerts, quality gate,
  deployment state, and a `gaps: list[EvidenceGap]` list for sources that failed or were missing. Every item carries its evidence ID.
- A deterministic `EvidenceIndex` (id → item) used later by the citation validator and the judge.

**Acceptance:** built by hand from the mocks for every scenario; the JSON schema is exported for prompts.

### A2 [P0, M] Evidence worker agents
- Four workers, each a compiled subgraph with its **own state schema** and **only its own tools**:
  Jira (scope and ticket hygiene), Change (diff, PR and review coverage, untracked commits), Quality (checks,
  alerts, Sonar), Deploy (slot state, deployment history, baseline health).
- Each returns **structured findings** (`with_structured_output`) that reference evidence IDs, plus a summary.
  Iteration and tool-call budgets per worker; when a budget is exhausted the run ends with a gap, not an exception.
- Prebuilt `create_react_agent` for one worker and a custom graph for another; write an ADR comparing them.

**Acceptance:** with the fake model, each worker's trajectory and output are asserted per scenario; a mocked
outage of one service produces a recorded gap, not a failed run.

### A3 [P0, L] Supervisor orchestration
- A supervisor graph that plans which workers to run, **fans out in parallel with `Send`**, merges findings
  through reducers, and decides on follow-up (for example "Quality found a CVE, so ask Change who touched that file").
- Handoffs with `Command(goto=..., update=...)`; `recursion_limit` and a global token and cost budget; a
  partial-evidence policy (which sources are required and which are optional).
- `run_id`, `thread_id` and `scenario` flow through config into every node.
- ADR: supervisor vs swarm vs plan-and-execute, and when `langgraph-supervisor` is enough.

**Acceptance:** an end-to-end run for all six scenarios with the fake model produces a `ReleaseEvidence`
plus findings; a real-model smoke test (marked `@pytest.mark.live`, skipped by default) runs `clean`.

### A4 [P0, M] Release document generation with verified citations
- A writer node produces a release document (Markdown, fixed template: summary, scope, changes by area,
  quality and security, deployment and migration notes, risks, rollback summary). **Every factual sentence cites
  evidence IDs** `[jira:PAY-102]`.
- A **citation validator**: unknown IDs, uncited claims in the required sections, and contradictions with
  deterministic facts (counts, versions, gate status) fail the document, and the writer gets one targeted revision.
- Stored in Postgres (`release_documents`, versioned) and published as a **draft** GitHub release.

**Acceptance:** documents for every scenario pass validation; an injected fake citation is caught; the `critical_cve` document lists the CVE under risks.

### A5 [P0, M] Risk assessment: policy as code plus LLM rationale
- `policies/change_policy.yaml`: deterministic rules (critical or high open alert → block; failing quality gate →
  block; untracked commit → block; DB migration → +1 risk; criticality-1 CI → minimum risk moderate; and so on).
- The rules engine produces `RiskAssessment{tier, score, blockers[], factors[]}` **before** any LLM involvement;
  the LLM only writes the rationale and **cannot lower the tier**.
- Decision: `proceed | block | proceed_with_conditions`.

**Acceptance:** matches `expected.decision` and `expected.risk` for all scenarios; property test that the LLM output never changes the tier.

### A6 [P0, M] Idempotent change-request creation
- Maps the document and risk onto ServiceNow fields (short description, description, type, risk, impact, CI,
  group, planned window, implementation, backout and test plans, justification).
- **Idempotent:** `correlation_id = payments-api@2.4.0`; query first, then create or update; safe under retries and re-runs of the same thread.
- The created CR number goes back to Jira (comment on the tickets) and into the audit log.

**Acceptance:** running the same release twice, or crashing after the POST and before the checkpoint, leaves exactly one CR.

### A7 [P0, L] Risk-tiered approval routing with durable human-in-the-loop
- Routing from policy: low → standard change (no human); moderate → Payments EM; high → CAB + AppSec; any
  security blocker → the change is not raised for approval and the run ends blocked.
- Creates `sysapproval_approver` records, notifies approvers (Teams card with the summary and a link), then
  **`interrupt()`**; the graph state is in the Postgres checkpointer (P1).
- **Resume via webhook:** the ServiceNow callback (`/_admin/servicenow/callback`) → `POST /webhooks/servicenow` →
  `Command(resume=decision)` on the right thread (looked up by correlation id). Handles rejection, partial
  approvals, duplicate callbacks (idempotent), and an SLA timeout → escalation notice.

**Acceptance:** kill the process while it waits for approval, restart, approve through the admin endpoint → the run
continues from the checkpoint; a duplicate callback is a no-op; rejection ends the run with the CR in state New.

### A8 [P0, M] Deployment, health verification and automatic rollback
- A rollback plan is created **before** deployment: swap-back steps, migration handling (additive → leave in place;
  destructive → manual), feature-flag kill switches, verification queries. It uses long-term memory (P2) for past incidents.
- Executes the slot swap, then watches production health against SLO thresholds (5xx rate, p95 latency) for a
  window; on breach, it **rolls back automatically** (pre-approved by the CAB as part of the plan), notifies, and
  updates the CR (close code and notes).
- A reflection pass checks the plan against the evidence (did it mention the PAY-102 migration?).

**Acceptance:** `failed_deploy` → the swap, the breach detection, the swap back, and the CR closed `unsuccessful` with notes; `clean` → closed `successful`.

---

## Epic P: Persistence and memory

### P1 [P0, M] Durable execution and audit trail on Postgres
- `AsyncPostgresSaver` checkpointer (connection pool, `.setup()` in migrations); threads keyed by `release:{service}@{version}`.
- App tables (Alembic): `runs` (status, scenario, timings, token and cost totals, outcome), `audit_events`
  (append-only: every write-tool call with actor, tool, arguments hash, result, evidence IDs, approval reference).
- Time travel: an endpoint or CLI to list checkpoints for a thread and replay from one.

**Acceptance:** a crash in the middle of a run followed by a resume finishes without repeating completed side
effects; every ServiceNow, GitHub or Azure write has an audit row.

### P2 [P1, M] Long-term agent memory (PostgresStore + pgvector)
**As** the copilot **I want** to remember lessons across releases **so that** past incidents shape risk and rollback plans.
- `AsyncPostgresStore` with a pgvector index (embeddings from F3); namespaces `("service", "payments-api", "lessons")`,
  `("team", "payments", "preferences")`.
- **Memory writer:** after a run ends (or from historical closed CRs at bootstrap), extract lessons
  (`Lesson{summary, trigger, evidence_ids, source_cr, confidence}`) using structured output; deduplicate by semantic similarity; record provenance.
- **Memory reader:** risk assessment and the rollback planner retrieve the top-k relevant lessons and must cite them (`memory:<id>`).
- Hygiene: no PII (redaction from G1 runs before writing), TTL and confidence decay, an admin CLI to list or delete.
- Short-term: message trimming and summarization for long worker conversations.

**Acceptance:** after bootstrapping from CHG0030012, the 2.4.0 rollback plan cites the 2.3.0 migration-lock lesson; re-running bootstrap creates no duplicates.

### P3 [P1, M] Policy and template RAG (pgvector)
- Ingest the change-management policy, release-document template and runbooks (Markdown in `knowledge/`) into a
  pgvector table: heading-aware chunking, metadata, hybrid search (Postgres FTS + vector, merged with RRF).
- The writer and risk nodes retrieve the policy sections they rely on and cite them (`policy:<doc>#<section>`).

**Acceptance:** retrieval tests (the right section in the top 3 for 10 labelled queries); the risk rationale cites the migration policy section.

---

## Epic G: Guardrails and security

### G1 [P0, M] Untrusted tool content: PII redaction and prompt-injection defense
- Every tool output passes a **sanitizer pipeline** before reaching a model: PII detection and redaction (Microsoft
  Presidio plus regexes for card, SSN, email and phone; Luhn check for cards), then an injection detector (heuristics plus a
  small LLM classifier, with the score recorded).
- **Spotlighting:** untrusted text is wrapped in delimiters with a system rule that it is data, never instructions.
- Flagged content is quarantined: kept out of prompts, surfaced in the document as "content withheld: suspected prompt injection in PAY-109".

**Acceptance:** `pii_in_ticket` → no PII in any LLM input, trace, document, CR, memory or notification (checked
against all of them); `prompt_injection` → no write call to an external host, no self-approval attempt, and the ticket is flagged.

### G2 [P0, M] Action guardrails and output checks
- An action policy layer in front of every write tool: an allowlist of hosts and tools, argument validation against
  the policy (no `approval`, no state jumps), confirmation that the action is approved where the policy needs it.
- Output groundedness check before publishing (claims ↔ evidence, reusing the A4 validator), plus a toxicity and safety check on notifications.
- Run NeMo Guardrails as an alternative input/output rail on the writer; ADR comparing it with the custom pipeline (latency, coverage, control).

**Acceptance:** red-team test suite (10+ cases: injections in comments, PR bodies and commit messages; exfiltration URLs; role-play) passes.

---

## Epic O: Observability and evaluation

### O1 [P0, M] OpenTelemetry tracing to Phoenix and Langfuse
- OTel setup with OpenInference LangChain instrumentation; exporters chosen through config:
  Phoenix (OTLP), Langfuse (OTLP endpoint or callback handler), LangSmith (`LANGSMITH_TRACING=true`).
- Span attributes: `run_id`, `thread_id`, `release`, `scenario`, `agent`, `tool`, token usage and cost; HTTP client spans from T1 nested under tool spans.
- PII-safe: the G1 sanitizer runs before export; prompts can be masked in traces through config.
- `compose.observability.yaml`: Phoenix, plus self-hosted Langfuse v3 (with its own Postgres, ClickHouse, Redis and MinIO).

**Acceptance:** one `clean` run shows the full tree (supervisor → workers → tools → HTTP) in both Phoenix and Langfuse with matching ids.

### O2 [P0, M] Golden dataset and offline eval harness
- Dataset built from scenario `expected` blocks plus hand-written reference answers (decision, risk, blockers, must-cite IDs, must-not actions).
- Deterministic metrics: decision accuracy, blocker recall and precision, citation validity, **trajectory checks** (required tools
  called, forbidden calls absent, using `/_admin/requests`), side-effect correctness (exactly one CR, right approvers), cost and latency.
- `uv run python -m release_copilot.evals run --model … --scenarios …` → a results JSON file plus a dataset and experiment in Langfuse and Phoenix.

**Acceptance:** the harness runs all scenarios N times (to measure variance) and writes a comparable report between two configurations.

### O3 [P0, M] LLM-as-a-Judge: relevance and faithfulness
- Judge prompts with explicit rubrics (1–5 plus reasons, structured output): **faithfulness** (every claim supported by
  the evidence index), **relevance** (the document covers what an approver needs for this release), plus completeness of the rollback plan.
- **Calibration:** 20 hand-labelled documents; report judge-to-human agreement (Cohen's kappa or Spearman); position and length bias checks; a different model family from the writer's.
- Implemented both as custom judges and as **DeepEval** metrics (`GEval`, `FaithfulnessMetric`); scores pushed to Langfuse.

**Acceptance:** agreement ≥ 0.6 on the calibration set; the judge catches 5 seeded faithfulness errors.

### O4 [P1, S] CI regression gate and online evaluation
- A GitHub Actions job (manual or on a label) runs the eval suite against a fixed model and fails on regression thresholds.
- Online: sample N% of production traces for judge scoring (Langfuse evaluators or a scheduled job), alert on drift.

**Acceptance:** a PR that breaks citation formatting fails the eval gate.

---

## Epic M: Model Context Protocol

### M1 [P1, M] MCP servers with the official SDK
- `mcp` (FastMCP) servers over **streamable HTTP**: `devops-evidence` (read tools from T5) and `itsm` (write tools),
  reusing the clients. Tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`); **resources**
  (release template, change policy); **prompts** (the release-notes prompt).
- Bearer-token auth on the servers; structured tool output (`outputSchema`).

**Acceptance:** MCP Inspector lists the tools, resources and prompts; contract tests call each tool through an MCP client session.

### M2 [P1, S] Agents consume tools over MCP
- `langchain-mcp-adapters` `MultiServerMCPClient`; config switch `tools_transport = direct | mcp`; per-agent tool filtering is kept.

**Acceptance:** the O2 eval results match between `direct` and `mcp` (same decisions on all scenarios).

---

## Epic S: Service and delivery

### S1 [P1, M] Agent API service
- FastAPI: `POST /releases/{service}/{version}/runs` (starts a background run, returns the run id), `GET /runs/{id}`,
  `GET /runs/{id}/events` (**SSE** stream of node and tool events via `astream_events`), `POST /runs/{id}/resume`,
  `POST /webhooks/servicenow`. API-key or JWT auth; a concurrency limit; graceful shutdown that leaves runs resumable.

**Acceptance:** start, stream, interrupt, resume and finish through the API alone; a restart of the service in the middle of a run resumes it.

### S2 [P1, M] Event-driven trigger via the Azure Service Bus emulator
- A mock "pipeline finished" webhook → publishes to a Service Bus queue (emulator container) → a worker consumes it with
  peek-lock, starts the run, and completes or dead-letters the message; duplicate detection by message id; max delivery count → DLQ.

**Acceptance:** 3 messages for the same release produce 1 run; a poison message ends up in the DLQ.

### S3 [P1, S] Containerized full stack
- Multi-stage Dockerfile (uv, non-root); `compose.yaml` profiles: `core` (Postgres, mocks, API, worker), `obs`
  (Phoenix, Langfuse), `bus` (Service Bus emulator). CI builds the images and runs a smoke test.

**Acceptance:** `docker compose --profile core --profile obs up` → a `clean` run through the API is visible in both tracing UIs.

---

## Epic X: Other frameworks (stretch)

### X1 [P2, M] OpenAI Agents SDK port
- Reimplement the Quality worker and the approval step with the OpenAI Agents SDK (agents, handoffs, guardrails, tracing),
  using the same MCP servers. ADR: Agents SDK vs LangGraph (state, durability, HITL, observability).

### X2 [P2, L] Microsoft Agent Framework (.NET)
- A .NET 9 console or ASP.NET service using Microsoft Agent Framework (Semantic Kernel successor) that runs
  the approval-routing workflow against the same MCP `itsm` server and Azure OpenAI. ADR from the point of view of a .NET shop.
