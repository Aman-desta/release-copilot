"""Token and cost accounting per (role, model), as a LangChain callback handler.

Attach one tracker per run through the run config; LangChain passes callbacks down to every nested model call:

    tracker = UsageTracker()
    await graph.ainvoke(state, config={"callbacks": [tracker]})
    tracker.summary()  # -> per role/model tokens and USD, plus totals

The role comes from the model's ``metadata["copilot_role"]``, which ``get_chat_model`` sets. The model name comes
from the provider's response, so a fallback model is billed as itself.
"""

import logging
import threading
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

from release_copilot.llm.pricing import price_for

log = logging.getLogger(__name__)
ROLE_KEY = "copilot_role"


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0  # None once any call had a model with unknown pricing

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class UsageTracker(BaseCallbackHandler):
    """Thread-safe: parallel branches (``Send`` fan-out) report into the same tracker."""

    raise_error = False  # accounting must never break a run

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._roles: dict[UUID, str] = {}
        self.by_role_model: dict[tuple[str, str], Usage] = {}

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[BaseMessage]], *, run_id: UUID,
        metadata: dict[str, Any] | None = None, **kwargs: Any,
    ) -> None:
        with self._lock:
            self._roles[run_id] = (metadata or {}).get(ROLE_KEY, "unknown")

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        with self._lock:
            role = self._roles.pop(run_id, "unknown")
            for generation in (g for batch in response.generations for g in batch):
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if not usage:
                    continue
                model = (message.response_metadata or {}).get("model_name") or (response.llm_output or {}).get("model_name", "unknown")
                self._add(role, model, usage["input_tokens"], usage["output_tokens"])

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        with self._lock:
            self._roles.pop(run_id, None)

    def _add(self, role: str, model: str, input_tokens: int, output_tokens: int) -> None:
        entry = self.by_role_model.setdefault((role, model), Usage())
        entry.calls += 1
        entry.input_tokens += input_tokens
        entry.output_tokens += output_tokens
        price = price_for(model)
        if price is None:
            if entry.cost_usd is not None:
                log.warning("no pricing for model %r; cost for role %r is unknown", model, role)
            entry.cost_usd = None
        elif entry.cost_usd is not None:
            entry.cost_usd += (input_tokens * price.input_per_million + output_tokens * price.output_per_million) / 1_000_000

    @property
    def total_cost_usd(self) -> float | None:
        costs = [u.cost_usd for u in self.by_role_model.values()]
        return None if any(c is None for c in costs) else round(sum(c for c in costs if c is not None), 6)

    def summary(self) -> dict[str, Any]:
        """JSON-friendly summary for run records (P1) and trace metadata (O1)."""
        return {
            "by_role_model": [
                {"role": role, "model": model, "calls": u.calls, "input_tokens": u.input_tokens,
                 "output_tokens": u.output_tokens, "cost_usd": None if u.cost_usd is None else round(u.cost_usd, 6)}
                for (role, model), u in sorted(self.by_role_model.items())
            ],
            "total_tokens": sum(u.total_tokens for u in self.by_role_model.values()),
            "total_cost_usd": self.total_cost_usd,
        }
