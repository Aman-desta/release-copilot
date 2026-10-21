"""Deterministic chat model for tests and offline runs (``RELEASE_COPILOT_LLM__PROVIDER=fake``).

- Replies come from a script (``responses``), consumed in order; the last one repeats once the script runs out.
- ``bind_tools`` works (the tools are recorded), so agents and ``with_structured_output`` run unchanged:
  script an ``AIMessage`` with ``tool_calls`` to answer as a tool call or a structured output.
- ``usage_metadata`` is filled (about 4 characters per token), so usage and cost tracking can be tested.
- A model name starting with ``fail`` raises ``openai.APIConnectionError``: use it to exercise fallbacks.
"""

from collections.abc import Sequence
from typing import Any

import httpx
import openai
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


class FakeChatModel(BaseChatModel):
    model_name: str = "fake-model"
    responses: list[AIMessage | str] = Field(default_factory=lambda: ["ok"])
    calls: list[list[BaseMessage]] = Field(default_factory=list)  # every prompt received, for assertions
    bound_tools: list[dict[str, Any]] = Field(default_factory=list)
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    def bind_tools(self, tools: Sequence[dict[str, Any] | type | BaseTool | Any], **kwargs: Any) -> "FakeChatModel":  # type: ignore[override]
        clone = self.model_copy(update={"bound_tools": [convert_to_openai_tool(t) for t in tools]})
        # The clone shares the script position and call log with the original, like one model behind many bindings.
        clone.calls = self.calls
        return clone

    def _next(self) -> AIMessage:
        reply = self.responses[min(self.cursor, len(self.responses) - 1)]
        self.cursor += 1
        return reply.model_copy(deep=True) if isinstance(reply, AIMessage) else AIMessage(content=reply)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.model_name.startswith("fail"):
            raise openai.APIConnectionError(request=httpx.Request("POST", "http://fake/v1/chat/completions"))
        self.calls.append(messages)
        message = self._next()
        prompt_tokens = sum(_tokens(str(m.content)) for m in messages)
        completion_tokens = _tokens(str(message.content) or str(message.tool_calls))
        message.usage_metadata = {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        message.response_metadata = {"model_name": self.model_name, "finish_reason": "stop"}
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output={"model_name": self.model_name})
