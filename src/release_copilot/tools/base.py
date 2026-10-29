"""Tool framework: one wrapper that turns an async function into a LangChain tool with production behaviour.

Every tool built with ``make_tool``:
- has a strict Pydantic argument schema (``extra="forbid"``); invalid arguments come back to the model as a
  structured ``InvalidArguments`` error instead of crashing the step;
- returns TWO things (``response_format="content_and_artifact"``):
  * **content**, the only part the model sees: compact JSON ``{"ok": true, "data": {...}}`` that fits the
    tool's token budget (generic truncation marks what was cut);
  * **artifact**, kept on the ``ToolMessage`` and never sent to the model: the full typed domain objects and
    their evidence ids, so the evidence index (A1) gets complete data without fetching it again;
- turns integration failures into ``{"ok": false, "error": {error_type, message, retryable}}`` the model can
  reason about. An unexpected vendor shape (``KeyError``, validation errors) becomes ``UnexpectedResponse``.
  Programming errors still raise: bugs must stay visible;
- carries metadata: ``access`` (``read`` | ``write``) and ``domain`` (``jira``, ``github``, ...). Write tools must
  take an ``idempotency_key`` argument; ``make_tool`` refuses to build one that doesn't.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import tiktoken
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, ValidationError

from release_copilot.integrations.errors import IntegrationError

log = logging.getLogger(__name__)
Access = Literal["read", "write"]
DEFAULT_BUDGET = 1500  # tokens of content per call
LONG_TEXT = 600  # characters; strings longer than this are shortened before list items are dropped
_ENCODING = tiktoken.get_encoding("o200k_base")  # GPT-4o/4.1 family tokenizer; close enough for budgeting


class ToolArgs(BaseModel):
    """Base for argument schemas: unknown arguments are an error, not silently ignored."""

    model_config = ConfigDict(extra="forbid")


class View(BaseModel):
    """Base for what the model sees: small, flat, no vendor JSON. ``None`` and empty values are dropped."""

    model_config = ConfigDict(frozen=True)


@dataclass(frozen=True)
class Evidence:
    id: str  # e.g. jira:PAY-102
    url: str | None = None


@dataclass
class ToolPayload:
    """What a tool function returns: the view for the model, plus evidence and full objects for the system."""

    view: BaseModel
    evidence: list[Evidence] = field(default_factory=list)
    objects: list[Any] = field(default_factory=list)  # full domain objects (Issue, Commit, ...)


@dataclass
class ToolArtifact:
    """Attached to the ToolMessage as ``artifact``; never shown to the model."""

    tool: str
    ok: bool
    evidence: list[Evidence] = field(default_factory=list)
    objects: list[Any] = field(default_factory=list)
    error: dict[str, Any] | None = None
    tokens: int = 0
    truncated: bool = False


def count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


TRUNCATED_NOTE = "output was shortened to fit the token budget; narrow the request for more"
# Tokens the envelope itself costs (worst case: with the truncation note), so data is fitted to what's left.
_ENVELOPE_TOKENS = count_tokens(_dump({"ok": True, "data": None, "truncated": TRUNCATED_NOTE}))


# ------------------------------------------------------------------------------------------- budgeting


def _is_marker(item: Any) -> bool:
    return isinstance(item, dict) and set(item) == {"omitted"}


def _real_len(items: list[Any]) -> int:
    return len(items) - (1 if items and _is_marker(items[-1]) else 0)


def _walk(node: Any, path: tuple[Any, ...] = ()) -> list[tuple[tuple[Any, ...], Any]]:
    """Every (path, value) in a JSON-like tree, parents before children."""
    out = [(path, node)]
    children = enumerate(node) if isinstance(node, list) else node.items() if isinstance(node, dict) else ()
    for key, child in children:
        out.extend(_walk(child, (*path, key)))
    return out


def _get(node: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        node = node[key]
    return node


def fit_to_budget(data: Any, budget: int) -> tuple[Any, bool]:
    """Shrink ``data`` until its JSON fits ``budget`` tokens. Deterministic, and it says what it cut:

    1. very long strings (over ``LONG_TEXT`` chars, e.g. a description) are halved first, marked ``…[truncated]``:
       keeping more items beats keeping one huge field;
    2. then, while some list has more than one item, the longest list drops a quarter of its items (at least
       one) from the end, and an ``{"omitted": n}`` marker records how many are gone;
    3. finally any remaining string of 40+ chars is halved, until it fits or nothing long is left.

    The budget is a *soft cap* against large outputs (hundreds of items, long descriptions). It never removes the
    last item of a list or cuts short fields like ids, so a tiny budget can end slightly above target.
    """
    truncated = False
    while count_tokens(_dump(data)) > budget:
        truncated = True
        long_texts = [(p, v) for p, v in _walk(data) if isinstance(v, str) and len(v) > LONG_TEXT]
        if long_texts:
            data = _halve(data, *max(long_texts, key=lambda pv: len(pv[1])))
            continue
        lists = [(p, v) for p, v in _walk(data) if isinstance(v, list) and _real_len(v) > 1]
        if lists:
            _, items = max(lists, key=lambda pv: _real_len(pv[1]))
            omitted = items.pop()["omitted"] if _is_marker(items[-1]) else 0
            cut = max(1, len(items) // 4)
            del items[len(items) - cut :]
            items.append({"omitted": omitted + cut})
            continue
        strings = [(p, v) for p, v in _walk(data) if isinstance(v, str) and len(v) >= 40]
        if not strings:
            break  # nothing left to cut meaningfully
        data = _halve(data, *max(strings, key=lambda pv: len(pv[1])))
    return data, truncated


def _halve(data: Any, path: tuple[Any, ...], text: str) -> Any:
    short = text[: len(text) // 2].rstrip() + " …[truncated]"
    if not path:
        return short
    _get(data, path[:-1])[path[-1]] = short
    return data


# -------------------------------------------------------------------------------------------- make_tool


def _error(error_type: str, message: str, *, retryable: bool = False, service: str | None = None, details: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"error_type": error_type, "message": message, "retryable": retryable}
    if service:
        err["service"] = service
    if details:
        err["details"] = details
    return err


def _validation_message(exc: Any) -> str:
    if isinstance(exc, ValidationError):
        problems = [{"field": ".".join(map(str, e["loc"])), "problem": e["msg"]} for e in exc.errors()]
        return _dump({"ok": False, "error": _error("InvalidArguments", "fix the arguments and call again", details=problems)})
    return _dump({"ok": False, "error": _error("InvalidArguments", str(exc))})


def make_tool(
    fn: Callable[[Any], Awaitable[ToolPayload]],
    *,
    name: str,
    description: str,
    args_schema: type[ToolArgs],
    access: Access,
    domain: str,
    budget: int = DEFAULT_BUDGET,
) -> BaseTool:
    """Wrap ``fn(args) -> ToolPayload`` as a LangChain tool (see the module docstring for the behaviour)."""
    if access == "write" and "idempotency_key" not in args_schema.model_fields:
        raise ValueError(f"write tool {name!r} must take an idempotency_key argument")

    async def run(**kwargs: Any) -> tuple[str, ToolArtifact]:
        args = args_schema(**kwargs)
        try:
            payload = await fn(args)
        except IntegrationError as exc:
            err = _error(type(exc).__name__, exc.message, retryable=exc.retryable, service=exc.service)
            content = _dump({"ok": False, "error": err})
            return content, ToolArtifact(tool=name, ok=False, error=err, tokens=count_tokens(content))
        except (KeyError, ValidationError, TypeError, ValueError) as exc:
            # The vendor answered with a shape we don't understand. Log the detail; tell the model only the category.
            log.warning("tool %s: unexpected response shape (%s: %s)", name, type(exc).__name__, exc)
            err = _error("UnexpectedResponse", f"{domain} returned data in an unexpected format; this source is unavailable for now")
            content = _dump({"ok": False, "error": err})
            return content, ToolArtifact(tool=name, ok=False, error=err, tokens=count_tokens(content))

        data = payload.view.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
        data, truncated = fit_to_budget(data, budget - _ENVELOPE_TOKENS)
        envelope: dict[str, Any] = {"ok": True, "data": data}
        if truncated:
            envelope["truncated"] = TRUNCATED_NOTE
        content = _dump(envelope)
        artifact = ToolArtifact(
            tool=name, ok=True, evidence=payload.evidence, objects=payload.objects, tokens=count_tokens(content), truncated=truncated
        )
        return content, artifact

    return StructuredTool.from_function(
        coroutine=run,
        name=name,
        description=description,
        args_schema=args_schema,
        response_format="content_and_artifact",
        handle_validation_error=_validation_message,
        metadata={"access": access, "domain": domain, "budget": budget},
        tags=[f"access:{access}", f"domain:{domain}"],
    )
