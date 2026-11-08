# ADR 0002: Orchestrating the evidence workers

- **Status:** accepted (A3, 2026-11-06)
- **Versions:** langgraph 1.2.19, langchain 1.4.8

## Context

A3 runs the four evidence workers from A2 (Jira, Change, Quality, Deploy) for one release, merges what they
found, and hands the result to A4 (the release document) and A5 (the approval decision). Three requirements
shape the choice:

1. **The work is known in advance.** A release review always needs the same four kinds of evidence. What varies
   is the content, and the occasional question that one worker's finding raises for another: Quality finds a
   critical CVE in a dependency, so ask Change which commits touched that dependency.
2. **The record must be reproducible.** What the document cites and what the approval policy reads can't depend
   on which tools a model happened to call. A model that skipped a scan must not produce a release that looks clean.
3. **Cost, failure and injection are bounded.** A run has a global token and cost budget, a source outage is a
   gap and not a crash, and text inside a ticket can't steer the run beyond what the code allows.

## Options

| | How it works | Fits here? |
|---|---|---|
| **Supervisor (chosen)** | A coordinator decides which workers run and collects their results; workers never talk to each other. In LangGraph: `Send` to fan out, reducers to merge, `Command` to hand off | Yes. The work is a known set of independent reviews, plus a bounded follow-up step |
| **Swarm** (`langgraph-swarm`) | Agents hand control to each other directly (`handoff` tools); whoever is active talks to the user | No. Nothing here is a conversation, the workers are independent (no reason to pass control), and the path a run takes depends on the model, so it's hard to bound or reproduce |
| **Plan-and-execute** | A model writes a plan (a list of steps), executors run it, and a replanner revises it | Partly. The plan is always the same four reviews, so a model writing it adds a call and a way to fail (skipping a scan) without adding information. We keep the useful part, the replanning step, as a bounded follow-up round |
| **Prebuilt `langgraph-supervisor`** | `create_supervisor(agents, model)`: a supervisor *agent* routes to worker agents through handoff tools, one at a time | Not for this graph: see below |

## Decision

A supervisor graph in which **code plans and models judge**:

```
START → collect → plan ──Send×N──→ worker (parallel) → review ──Send──→ worker (follow-ups) → review → finalize → END
```

- **collect**: the deterministic collector from A1 builds the `ReleaseEvidence`. It is the record: the
  document cites it and the policy reads it. Workers' findings are judgment *about* it, and their citations are
  checked against it.
- **plan**: in code. Every worker runs, except one whose every evidence section is unknown because its sources
  are down; it is skipped with a `blocked` gap instead of spending its budget on errors.
- **worker**: the A2 subgraphs, run in parallel with `Send`. Each returns only a report. The `reports` channel
  has a reducer (`operator.add`), so the parallel writes in one step are merged rather than rejected.
- **review**: only when a finding is high or critical (code decides whether to ask) does the supervisor's model
  propose follow-ups. Code then checks every proposal (`accept_follow_ups`): it has to be about evidence the run
  collected, for a worker that ran, at most one per worker and two in all. Accepted follow-ups are handed off
  with `Command(goto=[Send(...)], update=...)`. There is one follow-up round.
- **finalize**: the `ReleaseAssessment`: the evidence, the findings sorted by severity with each citation marked
  verified or not, and the coverage under the partial-evidence policy.

## Why not `langgraph-supervisor`

It is a good fit when the routing itself is the judgment, and that's a different problem from this one:

- **A model routes through handoff tool calls** (one handoff per turn by default). Our four reviews are
  independent and always run, in parallel. Routing them through a model costs supervisor calls and adds latency
  for a decision that never changes.
- **The model decides who runs.** Here that's policy: every review runs unless its sources are down. If routing
  is left to a model, it can quietly skip the security scan.
- **The workers' messages flow back into the supervisor's context** (configurable, but that's the design). We
  want reports in, raw tool output out (context isolation): it's smaller, cheaper, and keeps injected text and
  personal data out of the supervisor's prompt.
- **Our budgets and gaps are custom.** We need a global budget that turns into gaps, a skip rule, and validated
  follow-ups. In `create_supervisor` these would be middleware and hooks around a loop we don't control.

**When it is enough:** an assistant whose next step really depends on the conversation ("look up the ticket,
then maybe check the deploy"), with a handful of agents, no requirement that every agent always runs, and no
hard per-run budget. For example, a chat front end over the same tools would be a reasonable `create_supervisor`.

We did not measure `langgraph-supervisor` the way ADR 0001 measured the prebuilt worker; this comparison is from
its design (handoff tools, a supervisor agent loop), not from runs.

## Consequences

- **Evidence is fetched twice:** once by the collector, and again by the workers through their tools. That
  roughly doubles the upstream reads. It's the price of a reproducible record. If rate limits bite, an HTTP cache
  shared within a run is the fix (O3).
- **The supervisor's model is rarely called.** Only a severe finding triggers it, so most runs cost the workers'
  calls and nothing more.
- **Follow-ups are bounded by code, not by the prompt.** One round, two follow-ups at most, about collected
  evidence only. A prompt injection that reached a finding can make the model propose a strange follow-up, but
  not one about invented evidence or an unbounded chain of them.
- **The checkpointed state holds ids, not raw tool output.** Worker subgraphs keep no checkpoints of their own
  (they're read-only: a resumed run reruns them), and the supervisor keeps each report's evidence ids, not its
  artifacts. Personal data in tool output never reaches the checkpoint store, and the state survives a round
  trip with a strict serializer allowlist (`checkpoint_serializer()`).

## Interview summary

"We used a supervisor, but the plan is code: a release always needs the same four reviews, so a model choosing
them only adds a way to skip one. The four workers run in parallel with `Send`, and a reducer merges their
reports. The model's judgment is used where it adds something: when a finding is severe, it proposes follow-ups
for other workers, and code accepts only follow-ups about evidence we actually collected, at most two. Facts come
from a deterministic collector, so the record doesn't depend on what a model chose to look at. A global token and
cost budget is a callback that stops model calls, and every way a run can come up short (an outage, a spent
budget, an invalid answer) ends up as a gap that the evidence policy turns into complete, partial or insufficient."
