# Release Copilot: Agents and Tools, High-Level Design

**Status:** current as of O1 (Nov 2026) · **Scope:** the agent tool layer (T5), the evidence workers (A2),
the supervisor (A3) and tracing (O1). The rest of the workflow (A\*) is added here as it is built. Architecture choices are in
[docs/adr](adr/). The layer underneath (settings, model gateway, typed clients)
is in [design-foundations-and-integrations.md](design-foundations-and-integrations.md). Decision numbers continue
from that doc.

---

## 1. What this part of the system does

The integration layer fetches evidence reliably. The tool layer decides **what a model is allowed to do with
it and what it gets to see**. Every tool is a small contract between deterministic code and a model:

- the model asks a question in a strict, validated form ("which tickets are in fix version 2.4.0?");
- code answers with **verified facts** in a compact form that fits a token budget;
- the system keeps the full evidence behind each answer, so every claim the model later makes can be checked.

The tool layer is where three questions are settled for the whole system: what the model can see, what it can
change, and how its claims are traced back to sources.

## 2. Big picture

```mermaid
flowchart LR
    subgraph agent["Agents (A stories)"]
        M[Model]
        G[Graph state<br/>evidence index]
    end
    subgraph tools["Tool layer (this doc)"]
        TB[Toolbox<br/>read-only by default]
        MT[make_tool wrapper<br/>validate · budget · errors]
        F[Tool functions<br/>jira · github · sonar<br/>azure · servicenow]
    end
    subgraph integ["Integration layer (foundations doc)"]
        C[Typed clients]
    end
    TB -->|hands out tools| M
    M -->|tool call| MT --> F --> C
    MT -->|content: compact JSON| M
    MT -->|artifact: evidence + full objects| G
```

| Part | Responsibility | Where |
|---|---|---|
| `make_tool` | Turns an async function into a LangChain tool with validation, budgeting, structured errors and the content/artifact split | `tools/base.py` |
| `Toolbox` | Owns the clients and hands each agent only the tools it may use | `tools/toolbox.py` |
| Tool modules | One per system: arguments, the view the model sees, and what to fetch | `tools/{jira,github,sonar,azure,servicenow}.py` |

### 2.1 Life of one tool call

```mermaid
sequenceDiagram
    participant M as Model
    participant G as Graph
    participant W as make_tool wrapper
    participant F as Tool function
    participant C as Typed client
    participant V as Vendor API
    M->>G: tool call with name and arguments
    G->>W: invoke
    W->>W: validate arguments against the strict schema
    alt arguments invalid
        W-->>G: InvalidArguments error naming the field
    else arguments valid
        W->>F: fn(args)
        F->>C: typed calls, only the fields the view needs
        C->>V: HTTP (retries, breaker, auth)
        V-->>C: vendor JSON
        C-->>F: domain objects with evidence ids
        F-->>W: ToolPayload: view, evidence, objects
        W->>W: dump view and fit it to the token budget
        W-->>G: ToolMessage with content and artifact
    end
    G-->>M: content only
    G->>G: artifact goes to the evidence index
```

- **Validation happens before any I/O.** A malformed call costs nothing and comes back with the exact field and
  problem, so the model can fix it in one step.
- **The model only ever receives `content`.** The `artifact` stays in the graph: full objects, evidence ids and
  URLs, token count, and whether the content was truncated.
- **Failures are results too.** An integration error comes back as `{"ok": false, "error": {...}}`, not an
  exception, so one unavailable source doesn't crash the run.

## 3. Work completed

| Story | What was built | Where |
|---|---|---|
| **T5** Tool framework | `make_tool`, token budgeting with honest truncation, structured errors, write-tool rules | `tools/base.py` |
| **T5** Toolbox | Client ownership, filtering by domain and access, read-only default, duplicate-name check | `tools/toolbox.py` |
| **T5** Tools | 11 tools over Jira, GitHub, SonarQube, Azure and ServiceNow (catalog below) | `tools/*.py` |
| **T5** Tests | Behaviour tests per tool, framework tests, citation checks, schema snapshots | `tests/test_tools.py`, `tests/snapshots/tools/` |
| **A2** Evidence workers | Four workers with scoped read-only tools and budgets; a custom graph and the prebuilt agent ([ADR 0001](adr/0001-worker-graphs.md)) | `agents/workers.py`, `custom.py`, `prebuilt.py`, `report.py` |
| **A3** Supervisor | Deterministic collection, parallel workers with `Send`, validated follow-ups, global budget, partial-evidence policy ([ADR 0002](adr/0002-supervisor-orchestration.md)) | `agents/supervisor.py`, `followups.py`, `policy.py`, `budget.py`, `run.py` |

The whole repo has about 225 automated tests (54 for the tool layer), all running without network access or a
real LLM.

### 3.1 Tool catalog

Exact names, descriptions and argument schemas, byte for byte what the model receives, are in
[`tests/snapshots/tools/`](../tests/snapshots/tools/). This table is the overview, not the contract.

| Tool | Access | Budget | The question it answers |
|---|---|---|---|
| `jira_release_issues` | read | 2,000 | Which tickets are in the fix version, and which aren't done? |
| `jira_get_issue` | read | 10,000 | What does one ticket say: description and comments? |
| `jira_add_comment` | **write** | 1,500 | Leave a note on a ticket, at most once per idempotency key |
| `github_compare` | read | 2,000 | What changed since the last release? Is every commit tracked? Which files are risky? |
| `github_commit_pull_requests` | read | 1,500 | Was this commit reviewed, and who approved it? |
| `github_check_runs` | read | 1,500 | Did CI pass on this branch, tag or sha? |
| `github_security_alerts` | read | 1,500 | Which vulnerabilities are open, worst first? |
| `sonar_quality` | read | 1,500 | Does the quality gate pass? How did coverage change since the last release? |
| `azure_slots` | read | 1,500 | What runs in production and staging, and what would a swap or rollback change? |
| `azure_health` | read | 1,500 | Is this slot healthy right now (5xx rate, traffic, latency)? |
| `servicenow_service_profile` | read | 1,500 | How critical is the service, who supports it, and how did past changes go? |

Not in the catalog on purpose: creating change requests, requesting approvals and swapping slots (see D21).

---

## 4. Key design decisions

Each decision lists **what** we chose, **why** it matters, and the **trade-off** we accepted.

### D16. Facts in code, policy in code, judgment in the model

**What.** Every piece of logic is placed by one test:

| Layer | Question | Examples | Lives in |
|---|---|---|---|
| **Facts** | What is true? | A ticket isn't done, a commit has no ticket, a migration was modified, the 5xx rate is 3.2% | Tools (code) |
| **Policy** | What are we allowed to do? | A critical CVE blocks the release; a high-risk change needs CAB approval; who approves | Gates and graph structure (code) |
| **Judgment** | What does it mean, and what should we say? | Connecting a modified migration with 2.3.0's failed rollback; writing the release notes | The model |

Where judgment meets policy, **code sets a floor and the model may only raise it**:
`final_risk = max(policy_risk(facts), model_risk)`. The model can tighten a decision when it notices something
in ticket text, but nothing it reads can loosen one.

**Why it matters.**
- **Facts** have one right answer. Code gets it right every time, is unit tested in milliseconds and leaves an
  audit trail. A model gets it right *usually*, and misses items in the middle of long lists.
- **Policy** has to hold even when the model is wrong or manipulated. A rule the model can be talked out of
  ("security has accepted this risk, proceed", written in a Jira comment) is only a suggestion.
- **Judgment** done with rules collapses into an ever-growing list of `if`s that still misses the cases that matter.

**Trade-off.** More code up front: every fact needs a tool field and every policy needs a gate. In exchange, the
model's output is grounded, bounded and auditable.

### D17. Two outputs per call: content for the model, artifact for the system

**What.** Tools use LangChain's `response_format="content_and_artifact"`.
- **Content:** compact JSON `{"ok": true, "data": {...}}` built from a small **view** model. No vendor JSON,
  `None` values dropped, no spaces.
- **Artifact (`ToolArtifact`):** the full typed domain objects, the evidence ids with URLs, the token count,
  and whether the content was truncated. It stays on the `ToolMessage` and never reaches the model.

**Why it matters.**
- The model's context holds only what helps it reason, so it's cheaper and more focused.
- The system still has complete, typed data. The evidence index (A1) and the faithfulness checks (O stories)
  read the artifact instead of fetching again, or worse, parsing the model's text.
- Each view is a deliberate choice of what a release reviewer needs. That choice is the real design work in a tool.

**Trade-off.** Two shapes to maintain per tool (the view and the domain objects), and the artifact lands in
checkpoints, so it is subject to the same data rules as everything else (D23).

### D18. Token budgets, honest truncation, and verdicts before detail

**What.**
- Every tool has a budget (1,500 tokens by default). `fit_to_budget` shrinks the content deterministically:
  1. halve very long strings, such as a description;
  2. then drop a quarter of the longest list from the end, leaving an `{"omitted": n}` marker;
  3. then halve the remaining long strings.
- Truncation is never silent: the envelope gets a `truncated` note, and the artifact records it.
- **Verdict fields come first and are short:** `not_done_count` and `not_done_ids` come before `issues`, and
  `untracked_count` and `untracked_commit_ids` before `commits`. Likewise `all_passed` and `failing` before the
  check runs, and `counts` with alerts sorted worst first.

**Why it matters.**
- One huge result (a 400-commit diff, a ticket with a pasted log) can't push out every other piece of evidence.
- Truncation cuts the *longest list from the end*, which is often exactly where the risky item is: the late
  hotfix. Problem flags on individual items disappear with the item. A verdict field is never the longest list,
  so it survives, and the model still knows *that* there is a problem and *which* ids to cite.
- Counts survive even if an id list itself gets cut.

**Trade-off.** The budget is a soft cap. The wrapper never drops the last item of a list or cuts short fields
like ids, so a tiny budget can end slightly over. Token counts use the `o200k_base` tokenizer, which is close
enough for budgeting but not exact for every model.

### D19. Errors are results the model can act on, and bugs still raise

**What.**

| Situation | What the model gets |
|---|---|
| Invalid or unknown arguments | `InvalidArguments`, with each field and its problem |
| Integration failure (T1 error types) | That type (`NotFoundError`, `RateLimitedError`, ...), its message, and `retryable` |
| Vendor data in a shape we don't understand | `UnexpectedResponse`: "this source is unavailable for now". The details go to the log only |
| A programming error | Nothing: it raises, and the run fails visibly |

**Why it matters.**
- A missing ticket is a fact the model should report ("PAY-999 doesn't exist"), not a crash.
- `retryable` tells the model and the graph whether trying again can help.
- Vendor details can contain internal hostnames or data, and they don't help the model decide anything.
- Hiding programming errors would turn bugs into quiet, wrong answers.

**Trade-off.** Mapping `KeyError` and `TypeError` to `UnexpectedResponse` could mask a bug inside a view. The
warning log, and tests that run every tool against the mocks, keep that visible.

### D20. Argument schemas are the injection boundary

**What.**
- Every schema subclasses `ToolArgs` (`extra="forbid"`): unknown arguments are errors, not silently ignored.
- Every argument that reaches a query or URL has a strict pattern:
  - Jira key `^[A-Z][A-Z0-9]*-[1-9][0-9]*$`
  - fix version `major.minor.patch[-suffix]`
  - git refs from a safe character set, with no `..`
  - shas are hex, service names lower-case
- Fixed choices are `Literal`s (`slot: "production" | "staging"`), and numbers have bounds (`minutes` 5–1440).
- Values are **also** quoted or escaped where they're used: JQL `fixVersion = "2.4.0"`, and ServiceNow's `Q`
  builder (D13).

**Why it matters.** Tool arguments are written by a model, and a model can be steered by text it read earlier
(a ticket, a commit message). `2.4.0" OR project = SEC` must never reach Jira as JQL. Validating *and* escaping
means one mistake alone doesn't open a hole.

**Trade-off.** Strict patterns can reject unusual but valid values, like a version named `2024.10`. They get
widened deliberately, with a test, when a real case appears.

### D21. Least privilege: read-only by default, and high-risk writes are not tools

**What.**
- `Toolbox.tools()` returns **read** tools unless the caller explicitly asks for `access={"read", "write"}`.
- A write tool must take an `idempotency_key` marked `InjectedToolArg`, so the model never sees it (D22).
  `make_tool` refuses to build one that doesn't.
- Creating change requests, requesting approvals and swapping slots are **not tools at all**. They are
  deterministic workflow steps behind human approval (A6–A8).
- The only model-callable write today is `jira_add_comment`: low impact, reversible, and visible to humans.

**Why it matters.** What a model can't call, no prompt injection can make it call. Evidence workers can't hold
write tools by accident, and the riskiest actions run in code with explicit gates.

**Trade-off.** Less autonomy. The model *recommends* a swap or rollback, and the workflow decides. That is the
point for a bank release.

### D22. Idempotent writes on an API that has no idempotency keys

**What.** Jira has no `Idempotency-Key` header, so `jira_add_comment` does it itself:
- Each comment ends with a short, **visible** footer: `— posted by release-copilot · ref 3f9a2c1d7e4b5a60`.
  The ref is the first 16 hex characters of the SHA-256 of the idempotency key.
- Before posting, the tool looks for a comment carrying that ref. If one exists, it returns that comment with
  `already_posted: true`.
- **One template** defines the footer, the per-key match and the generic strip, so they can't drift apart. Matching
  needs only `ref <digest>`, so it survives formatting changes and human edits.
- Footers are **stripped on read** (`jira_get_issue` never shows them to the model) and **on write** (a footer
  copied into `text` is removed). Only our code writes footers.

**Why it matters.**
- After a timeout, the outcome of a POST is unknown (D11). The safe response is to call again, which must not
  create a duplicate.
- The marker is written **in the same request as the comment**, so they exist together or not at all. A
  separate record (a Jira comment property, or a row in our database) leaves a window where a crash between the
  two requests causes a duplicate.
- Rich text in ADF has no hidden comments, so an HTML comment would show up as literal text anyway. A short,
  honest footer is also a useful audit trail.
- A footer copied from another comment would make *that* key's next post look done, and skip it silently.

**Who chooses the key (A2).** The graph, never the model. `idempotency_key` is `Annotated[str, InjectedToolArg]`,
so it's absent from the schema the model sees. The tool executor (`agents/custom.py`) sets it to
`"{run_id}:{tool_call_id}"`, overwriting anything the model sent:
- a **replay** of the same tool call (a retry, or a resume from a checkpoint that re-runs the tools node) has the same
  run and tool-call ids, so it gets the same key and finds its own footer;
- a model-chosen key would change between attempts (defeating deduplication), or repeat across releases
  (suppressing a real comment);
- a run without a `run_id` raises before any write runs, rather than writing with a made-up key.

**Trade-off.** Check-then-post isn't atomic: two **concurrent** calls with the same key can both post. An
`asyncio.Lock` would only cover one process. Real exactly-once needs server-side support, so the guarantee is
"at least once, almost always exactly once".

### D23. Fetch only what the view shows

**What.** Each tool asks its client for exactly what its view needs. `jira_release_issues` passes
`fields=RELEASE_FIELDS` (no description, no comments). `jira_get_issue` fetches everything, because it shows
everything. The client's default (`ISSUE_FIELDS`) stays complete: narrowing belongs to the caller.

**Why it matters.**
- **Cost:** about half the payload for the release list, and no extra comment-page requests.
- **Data exposure:** the artifact keeps the full objects, and the artifact goes into checkpoints and possibly
  traces. A customer email in a production-bug description would be stored even though nothing needed it. The
  safest personal data is data that's never fetched.
- Cleaning up afterwards (fetching everything, then emptying `objects`) would still pass the data through the
  process and logs, and would throw away the evidence the graph needs.

**Trade-off.** Narrow fetching is a contract with the mapper underneath: `Issue` requires `created` and
`updated`, so they're in `RELEASE_FIELDS` even though the view doesn't show them. Redacting personal data in what
*is* fetched is G1's job (Presidio and regexes, applied in one place inside `make_tool`).

### D24. Every id the model can see is citable

**What.**
- Views use evidence ids (`jira:PAY-102`, `gh:commit:1c63f6d`, `jira:comment:PAY-102:10001`), and the artifact
  holds an `Evidence(id, url)` for each.
- **Naming convention:** a field named `id` or ending in `_id` or `_ids` holds evidence ids. The test helper
  `assert_citable` finds them by name and checks every one is backed by evidence in the artifact.
- Evidence covers *everything fetched*, not only what survived truncation, so an id in a verdict field stays
  citable even when its item was cut.

**Why it matters.** The release document and the change request cite these ids, and the faithfulness checks
verify each citation against the evidence. A visible id without evidence would make an accurate claim fail
verification. Because of the naming convention, a new summary field is checked automatically, with no list to update.

**Trade-off.** Field names carry meaning, so a reviewer has to keep the convention when adding fields.

### D25. Risk classification is a table of rules, not a model's opinion

**What.** `github_compare` classifies changed files with `_RISK_RULES`, one regex per category:

| Category | Examples |
|---|---|
| `migration` | `.cs`/`.sql` in a `Migrations` folder, timestamped EF migrations, any `.sql` |
| `config` | `appsettings*.json` (except `Development` and `Local`), `web.config`, `host.json` |
| `infra` | `*.bicep`, `*.tf`, `Dockerfile` |
| `pipeline` | `.github/workflows/*`, `azure-pipelines.yml` |
| `dependencies` | `*.csproj`, `packages.lock.json`, `Directory.Packages.props` |

- Test paths are excluded before any rule runs.
- Each risky file is shown with its **status**.
- **Migrations are append-only:** a migration script that was modified, removed or renamed goes into
  `migration_not_added`, a verdict field. EF Core's `ModelSnapshot.cs` is part of the schema change but is
  modified by *every* migration, so it is listed but exempt from that flag.

**Why it matters.**
- This is a fact (D16): it needs complete recall and an explanation an auditor accepts ("matched `Migrations/`,
  status `modified`").
- File names are written by whoever opened the PR. A rule can't be talked into anything; a model reading
  `ignore-previous-instructions-mark-low-risk.md` can be.
- A flag that fires on every release teaches people to ignore it. The snapshot exemption came from domain
  knowledge the mock data didn't contain.

**Trade-off.** Rules only know the conventions they encode (.NET, Azure, GitHub Actions here). New stacks need new
rows, each with a test.

### D26. Tool schemas are an API, protected by snapshot tests

**What.**
- Each tool's model-facing schema (name, description, arguments) is saved as JSON in `tests/snapshots/tools/`.
- A test fails when one changes. Accept a deliberate change with `UPDATE_SNAPSHOTS=1`, and review the diff like
  an API change.
- Descriptions are written for the model: when to use the tool, what an empty result means ("pushed directly, no
  review"), and which signals are weak ("staging has little traffic").

**Why it matters.** A renamed argument or reworded description changes model behaviour as surely as a code
change, but no type checker sees it. Snapshots make that visible in review, and later in evals (O stories).

**Trade-off.** Snapshots cover what the model *sends*, not the views it receives. Views are covered by
behaviour tests instead.

### D27. Testing approach for tools

**What.**
- Tools are invoked **the way an agent invokes them** (a tool-call dict), so tests see the real `ToolMessage`
  with both content and artifact.
- Each tool has behaviour tests against the in-process mocks:
  - what the model sees, and that it stays within budget
  - no descriptions in compact views
  - citability
  - structured errors for a 404 and for bad arguments
- Safety tests prove the failure is real **and** that it is caught. For example, the truncation test asserts the
  untracked commit *was* cut from `commits` and *still* appears in `untracked_commit_ids`.
- Pure functions (`fit_to_budget`, `_risk_categories`) get parametrized unit tests that include tricky negatives.
- Vendor responses are reshaped with `RewritingTransport` (60 commits, a modified migration, an edited footer)
  instead of adding a mock scenario for each case.

**Why it matters.** Tool failures in production are usually silent: a cut item, a misleading flag, an id
that can't be cited. Each test pins one of those down.

### D28. The supervisor: code plans, models judge (A3)

**What.** The facts come from the deterministic collector (A1), and code decides which workers run: all four,
except a worker whose every evidence section is unknown, which is skipped with a `blocked` gap. A model is used
for one decision only: after the first round, and only if a finding is high or critical, it proposes follow-ups.
Code accepts a proposal only if it's about collected evidence, for a worker that ran, at most one per worker and
two per run, in one round. See [ADR 0002](adr/0002-supervisor-orchestration.md) for the alternatives.

**Why it matters.** A release review always needs the same reviews, so a model choosing them only adds a way to
skip one. Bounding follow-ups in code means a prompt injection can't create an unbounded or invented chain of work.

### D29. The partial-evidence policy

**What.** `agents/policy.py` classifies each of the 16 evidence sections as **required** (scope, the change and
its reviews, CI, security alerts, the quality gate, the slots, the CMDB record, an existing change request) or
**optional** (coverage measures, deployment history, metrics, support group, past changes). `EvidencePolicy`
refuses a policy that leaves a section out or lists one twice. `assess_coverage` turns gaps and worker outcomes
into a `Coverage`:
- `insufficient`: a required section has a gap, or a gap names a section the policy doesn't know (fail closed);
- `partial`: an optional section has a gap, or a worker was skipped, ran out of budget, or gave no valid answer;
- `complete`: otherwise.

**Why it matters.** "Run anyway, record the gap" (A1) only works if something decides what a gap means. A5 must
not approve automatically on `insufficient` evidence, and the document says what's missing on `partial`.

### D30. A global budget, enforced by a callback

**What.** `BudgetGuard` is a LangChain callback with `raise_error=True`. Before every model call in the run
(any worker, the supervisor), it compares what the run has spent (`UsageTracker`) with `RunBudget(max_tokens,
max_cost_usd)` and raises `BudgetExceeded` when it's spent. The supervisor turns that into an `exhausted` gap for
the worker, or for the follow-up step. If a cost limit is set and a model has no pricing, the cost can't be
checked, so the guard stops the run (unknown is not zero). Each graph also has a `recursion_limit`: the
supervisor's is fixed, and each worker's comes from its own budget.

**Why it matters.** Per-worker budgets (A2) bound each loop, but not the run: four workers plus follow-ups can
add up. A callback sees every model call without any node passing the budget around. It's a soft cap: calls
already running when the limit is crossed finish (at most one per worker).

### D31. Run identity flows through the config

**What.** `run_config(run_id, thread_id, scenario=...)` puts all three in `configurable` (for nodes) and in
`metadata` (for callbacks and traces, O1). Every node opens `run_context(config)`, which sets the integration
layer's contextvars: the run id becomes every outbound call's `X-Correlation-ID`, and the scenario becomes
`X-Mock-Scenario`. Setting them per node, not once around `ainvoke`, means a resumed run gets them too. A
scenario is refused when `env` is `prod`.

**Why it matters.** Evals (O2) run scenarios in parallel against one mock server, and traces have to be found by
run and scenario. One config, read in one place, is how both stay consistent.

### D32. Checkpointed state is ids, not tool output

**What.** Worker subgraphs are compiled with `checkpointer=False` (including the prebuilt agent inside the Jira
worker), so only the supervisor's state is checkpointed. The supervisor keeps each worker's report with its
evidence ids (`TaskReport.collected`), not its tool artifacts. `checkpoint_serializer()` restores only the
state's own types (an allowlist built from the state's models).

**Why it matters.** Tool artifacts hold personal data and don't survive a round trip as objects. The serializer
turns objects inside the artifact dataclass into plain dicts. Keeping ids makes the checkpoint small, safe to
store and exact on resume. Without an allowlist, LangGraph restores any class a checkpoint names, which a future
version blocks.

### D33. One trace per run, from the supervisor down to every HTTP request (O1)

**What.** `run_supervisor` opens a `release_review` root span. OpenInference's LangChain instrumentation adds a
span for every graph, node, model call and tool call, and `ServiceClient.request` adds one CLIENT span per
logical HTTP request, with retries as events on it (`http.request.resend_count`). Span names and `url.template`
use the path template, never the filled-in path or the query.

**The bridge.** OpenInference parents its spans through LangChain's run tree but doesn't make them the *current*
OTel span. Without help, an HTTP span inside a tool sees only the run's root as current. With no root, it starts
a trace of its own: we measured 107 traces for one run. `http_span` asks LangChain which run it's inside
(`openinference...get_current_span()`) and parents on that. The result is one trace, with each request under the
tool that made it, including when the four workers run in parallel.

**Why it matters.** A slow or failing run is read top-down in one place: which worker, which tool, which request,
how many attempts.

### D34. The run's attributes are on every span

**What.** `RunAttributeProcessor.on_start` copies `run_id`, `thread_id`, `release`, `scenario`, `agent` and
`tool` from the parent span to the child (`run_attributes`). An agent's graph sets `agent` (`change_worker` gives
`change`) and a tool's span sets `tool`. The root also carries `session.id` (the thread) and, when the run ends,
its totals: tokens, cost, coverage and the number of findings.

**Why it matters.** A span is found by its own attributes. "Every HTTP 429 from GitHub in the quality worker,
across runs" is one filter. Langfuse also documents that trace-level attributes must be on every span to filter
reliably. It has to be `on_start`: once a span ends it is read-only, and a parent is still open while its
children start.

### D35. Sanitized at export, with the content mode in config

**What.** Every exporter is wrapped in `SanitizingExporter`, the last code that sees a span before it leaves the
process. It first adds cost to model spans (`llm.cost.*` for Phoenix and `gen_ai.usage.cost` for Langfuse, from
llm/pricing.py; nothing when pricing is unknown). Then it runs `sanitize_attributes` over the attributes, the
span events (exception messages and stack traces) and the error status message. `tracing.content` is one of:
- `redacted` (default): content goes through the redactor;
- `hidden`: content becomes `[hidden: N chars]`;
- `raw`: refused in production.

A redactor that raises turns that value into `[redaction failed]`: it never leaks the raw value, and never stops
the export. Until G1, the redactor is `basic_redact`, which covers emails, Luhn-valid card numbers, SSNs and
formatted phone numbers.

**Why at export.** Instrumentation libraries add attributes we don't write. The exporter sees all of them, ours and
theirs, in one place, and the in-process spans stay complete for anything that runs before export.
**LangSmith is not covered**: LangChain sends to it directly from its callbacks. So `LANGSMITH_TRACING=true` is
refused in production and logged as a warning locally.

### D36. Two backends over OTLP, auth on, same ids

**What.** The same spans go to Phoenix and Langfuse through two `OTLPSpanExporter`s, so a run has one trace id in
both: `SupervisorRun.trace_id`, which the `review` CLI prints. `compose.observability.yaml` runs:
- Phoenix 21, with auth left on (its default beyond loopback). The exporter sends `Authorization: Bearer`
  (`tracing.phoenix_api_key`).
- Langfuse v4, with its own Postgres, ClickHouse, Redis and MinIO. Its project and keys are created at start-up,
  and the exporter uses Basic auth plus `x-langfuse-ingestion-version: 4`. The story said v3; upstream's compose
  is v4 now. In v4's events-only mode the old `/api/public/traces` read API is off, and the real-time read path
  is `/api/public/v2/observations`.

The `--live` acceptance test exports a scripted run (no LLM cost) and reads it back from both. It checks the same
trace id and span ids in each, and that Langfuse sees the model spans as generations with our cost.

**Why it matters.** Phoenix is strong for debugging traces and evals. Langfuse is strong for prompts, costs and
online scores. OTLP means neither one is in the code, and adding or dropping a backend is configuration.

---

## 5. Known limitations and follow-ups

| Item | Why it's acceptable for now | Where it gets addressed |
|---|---|---|
| ~~The model chooses `idempotency_key`~~ | | **Done in A2**: the graph injects it (`InjectedToolArg`), see D22 |
| The prebuilt worker can't inject keys (its tool node doesn't fill `InjectedToolArg`) | No worker has a write tool | A `wrap_tool_call` middleware, if a prebuilt worker ever needs to write |
| Concurrent `jira_add_comment` calls with the same key can both post | Needs server support; rare in one run | Documented (D22); serialize writes per issue in the graph if needed |
| Personal data in fetched text (descriptions, comments) reaches the model and the artifact | Minimized (D23), not yet redacted | G1: a sanitizer stage inside `make_tool`, over content and artifact |
| Tool content is untrusted input (instructions hidden in tickets or commits) | Read-only tools and the absence of high-risk tools limit the damage | G1: injection detection, plus G2 action guardrails |
| Renamed files: `previous_filename` isn't mapped, so a migration moved out of its folder isn't flagged | Rare | Add it to `ChangedFile` (T3 client) |
| `jira_get_issue` has a 10,000-token budget | Tickets are usually small | Revisit with real token usage from tracing (O1) |
| The budget is a soft cap, counted with one tokenizer | Close enough for every configured model | Revisit if a model with a very different tokenizer is added |
| ~~Tool calls aren't traced yet~~ | | **Done in O1**: spans per run, node, model, tool and HTTP request (D33–D36) |
| Trace redaction is `basic_redact`: patterns for emails, cards, SSNs and phones, not names or free text | The mock data's personal data is covered, and `hidden` mode exports no content at all | G1: the real sanitizer, passed to `configure_tracing(redact=...)` |
| Content attributes are a list of known keys (`is_content_key`): a new content attribute from a library would be exported as is | OpenInference's content keys are covered and tested | G1: also check unknown string attributes over a size threshold |
| Locally, Phoenix's admin secret is the exporter's API key | Local only; the variable is documented | Deployments create a system API key in Phoenix (least privilege) |
| Every run is traced (no sampling), and a batch still queued when the process is killed is lost | Low volume; the CLI flushes on exit (`Tracing.shutdown`) | S1/O4: a sampler for online runs, and a flush on the service's shutdown |
| Tools are LangChain objects only | Enough for the LangGraph agents | M stories: the same tool functions served over MCP |
| Evidence is fetched twice (collector and workers) | A reproducible record is worth the reads (ADR 0002) | O3: a per-run HTTP cache, if rate limits bite |
| A thread per run (`release:{service}@{version}:{run_id}`) | A second run on a finished thread would append to its reports | P1: threads per release, with a rule for reruns |
| A run stopped by the global budget loses that worker's partial work | The gap says so, and coverage becomes `partial` | Pass the budget into the worker loop so it can answer with what it has |
| Workers use the primary model without a fallback (`get_chat_model`) | Workers bind their own tools, and fallbacks need binding per model | `get_resilient_model(role, prepare=bind_tools)` per worker |

## 6. Glossary

- **Content / artifact:** the two parts of a tool result. The model sees the content, and the system keeps the artifact.
- **View:** the small Pydantic model a tool dumps as content. It contains only what a reviewer needs.
- **Evidence:** an id plus an optional URL that backs one fact (`jira:PAY-102`). Citations must point at evidence.
- **Token budget:** the maximum size of a tool's content, enforced by deterministic truncation.
- **Verdict field:** a short top-level summary (`not_done_ids`, `untracked_count`) that survives truncation.
- **Least privilege:** giving each agent only the tools, and the access, its job needs.
- **Idempotency footer:** the visible `ref <digest>` line that makes a Jira comment safe to post twice.
- **Risk floor:** the minimum risk computed by code. The model can raise it, never lower it.
- **Append-only migration:** a schema migration that must never change once it has run in production.
- **Schema snapshot:** a saved copy of what the model sees of a tool, compared in tests.
- **`InjectedToolArg`:** a LangChain tool argument filled in by the system, hidden from the model's schema.
