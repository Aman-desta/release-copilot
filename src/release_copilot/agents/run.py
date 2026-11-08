"""What identifies a run, and how it reaches every node: ``run_id``, ``thread_id`` and ``scenario`` in the config.

    config = run_config(run_id="run-42", thread_id="release:payments-api@2.4.0", scenario="clean")

- ``configurable`` carries them to every node, including worker subgraphs (LangGraph passes the config down);
- ``metadata`` carries them to every callback and trace span (O1), so a span can be found by run or scenario;
- each node opens ``run_context(config)``, which sets the integration layer's contextvars: the run id becomes the
  ``X-Correlation-ID`` of every outbound call, and the scenario becomes ``X-Mock-Scenario`` (evals only).

Setting the contextvars in each node, not once around ``ainvoke``, matters: a run resumed from a checkpoint, or
started by a server that only has the config, still gets them.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field

from release_copilot.config import get_settings
from release_copilot.evidence.model import ReleaseIdentity
from release_copilot.integrations.context import correlation, mock_scenario

# Supervisor steps: collect, plan, workers, review, follow-ups, review, finalize; doubled for headroom. Worker
# subgraphs get their own limit from their budgets (``worker_recursion_limit``).
SUPERVISOR_RECURSION_LIMIT = 16


class RunInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, description="One execution. Also the idempotency-key prefix for writes (D22).")
    thread_id: str = Field(min_length=1, description="The checkpoint thread the run's state is saved under.")
    scenario: str | None = Field(default=None, description="The mock scenario, for evals; None in real runs.")


def release_thread_id(identity: ReleaseIdentity, run_id: str) -> str:
    """A thread per run, for now. P1 keys threads by release (resume a run instead of starting over), which needs
    a rule for a second run on a finished thread: the reducers would append to the first run's reports."""
    return f"release:{identity.correlation_id}:{run_id}"


def run_config(
    run_id: str,
    thread_id: str,
    *,
    scenario: str | None = None,
    callbacks: Sequence[BaseCallbackHandler] = (),
    recursion_limit: int = SUPERVISOR_RECURSION_LIMIT,
) -> RunnableConfig:
    info = RunInfo(run_id=run_id, thread_id=thread_id, scenario=scenario)
    if scenario is not None and get_settings().env == "prod":
        raise ValueError("a mock scenario can't be set in production")
    values: dict[str, Any] = info.model_dump()
    return {"configurable": dict(values), "metadata": dict(values), "callbacks": list(callbacks), "recursion_limit": recursion_limit}


def run_info(config: RunnableConfig) -> RunInfo:
    """The run's identity from a node's config. Missing ``run_id`` or ``thread_id`` is a wiring bug: it raises."""
    configurable = config.get("configurable", {})
    return RunInfo(run_id=configurable.get("run_id", ""), thread_id=configurable.get("thread_id", ""), scenario=configurable.get("scenario"))


@contextmanager
def run_context(config: RunnableConfig) -> Iterator[RunInfo]:
    info = run_info(config)
    with correlation(info.run_id), mock_scenario(info.scenario):
        yield info
