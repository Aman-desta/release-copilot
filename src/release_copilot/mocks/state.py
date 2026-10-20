"""Process-wide mock state: one mutable world per scenario, fault rules, request log, notifications.

The scenario for a request comes from the ``X-Mock-Scenario`` header if present, otherwise from the active
default. Worlds are built lazily and kept until reset, so writes (change requests, comments, slot swaps)
stay visible to later reads in the same scenario.
"""

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request

from release_copilot.mocks import scenarios

current_scenario: ContextVar[str] = ContextVar("current_scenario", default="clean")


@dataclass
class FaultRule:
    path_prefix: str
    kind: str  # "status" | "latency" | "timeout"
    status: int = 503
    delay_ms: int = 0
    times: int | None = None  # None = every matching request
    method: str | None = None
    scenario: str | None = None
    retry_after: int | None = None
    id: int = 0
    hits: int = 0

    def matches(self, method: str, path: str, scenario: str) -> bool:
        if not path.startswith(self.path_prefix):
            return False
        if self.method and self.method.upper() != method.upper():
            return False
        if self.scenario and self.scenario != scenario:
            return False
        return self.times is None or self.hits < self.times


@dataclass
class MockState:
    active: str
    worlds: dict[str, dict[str, Any]] = field(default_factory=dict)
    faults: list[FaultRule] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    azure_tokens: dict[str, float] = field(default_factory=dict)  # token -> expiry (epoch seconds)
    _next_fault_id: int = 1

    def world(self, scenario: str | None = None) -> dict[str, Any]:
        name = scenario or current_scenario.get()
        if name not in scenarios.SCENARIOS:
            raise HTTPException(400, f"Unknown mock scenario {name!r}; known: {sorted(scenarios.SCENARIOS)}")
        if name not in self.worlds:
            self.worlds[name] = scenarios.build(name)
        return self.worlds[name]

    def reset(self, scenario: str | None = None) -> None:
        if scenario:
            self.worlds.pop(scenario, None)
        else:
            self.worlds.clear()
            self.faults.clear()
            self.requests.clear()
            self.notifications.clear()
            self.azure_tokens.clear()

    def add_fault(self, rule: FaultRule) -> FaultRule:
        rule.id = self._next_fault_id
        self._next_fault_id += 1
        self.faults.append(rule)
        return rule

    def match_fault(self, method: str, path: str, scenario: str) -> FaultRule | None:
        for rule in self.faults:
            if rule.matches(method, path, scenario):
                rule.hits += 1
                return rule
        return None

    def log_request(self, entry: dict[str, Any]) -> None:
        self.requests.append(entry)
        del self.requests[:-2000]  # bounded

    def issue_azure_token(self, token: str, ttl: int) -> None:
        self.azure_tokens[token] = time.time() + ttl


def get_state(request: Request) -> MockState:
    return request.app.state.mock


def get_world(request: Request) -> dict[str, Any]:
    return get_state(request).world()
