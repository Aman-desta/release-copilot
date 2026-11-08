"""The run's global token and cost budget, enforced before every model call.

Each worker has its own call budgets (A2); this caps the whole run, across parallel workers and the supervisor.
``BudgetGuard`` is a callback, so it sees every model call in the run without any node passing it around:

    tracker = UsageTracker()
    config = {"callbacks": [tracker, BudgetGuard(RunBudget(max_tokens=200_000), tracker)]}

The check runs *before* a call and uses what the run has spent so far, so it is a soft cap: calls already in
flight when the limit is crossed still finish (at most one per running worker). A call that would start over the
limit raises ``BudgetExceeded``; the supervisor turns that into an ``exhausted`` gap, never a crashed run.
"""

from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from release_copilot.llm.usage import UsageTracker


class BudgetExceeded(Exception):
    """The run's token or cost budget is spent. An expected outcome, not a bug: callers record it as a gap."""


@dataclass(frozen=True)
class RunBudget:
    max_tokens: int = 200_000
    max_cost_usd: float | None = 1.00  # None: no cost limit (tokens still apply)

    def __post_init__(self) -> None:
        if self.max_tokens < 1 or (self.max_cost_usd is not None and self.max_cost_usd <= 0):
            raise ValueError("budget limits must be positive")


class BudgetGuard(BaseCallbackHandler):
    # The exception must reach the model call, or the guard is only a log line. Not run_inline: LangChain awaits
    # inline handlers one by one *before* starting the others, so a raise there leaves their coroutines unawaited.
    raise_error = True

    def __init__(self, budget: RunBudget, tracker: UsageTracker) -> None:
        self.budget = budget
        self.tracker = tracker

    def on_chat_model_start(self, serialized: dict[str, Any], messages: Any, **kwargs: Any) -> None:
        self.check()

    def check(self) -> None:
        tokens = self.tracker.total_tokens
        if tokens >= self.budget.max_tokens:
            raise BudgetExceeded(f"the run's token budget is spent ({tokens} of {self.budget.max_tokens} tokens)")
        if self.budget.max_cost_usd is None:
            return
        cost = self.tracker.total_cost_usd
        if cost is None:
            # Unknown is not zero: a model without pricing could spend anything, so a cost limit can't be kept.
            raise BudgetExceeded("the run's cost can't be checked: a model without pricing was used (llm/pricing.py)")
        if cost >= self.budget.max_cost_usd:
            raise BudgetExceeded(f"the run's cost budget is spent (${cost:.4f} of ${self.budget.max_cost_usd:.2f})")
