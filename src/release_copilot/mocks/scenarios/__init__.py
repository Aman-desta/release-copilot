"""Scenario registry: ``build(name)`` returns a fresh, independent world for that scenario."""

import copy
from typing import Any

from release_copilot.mocks.scenarios.base import build_world
from release_copilot.mocks.scenarios.variants import SCENARIOS, Scenario


def build(name: str) -> dict[str, Any]:
    world = copy.deepcopy(build_world())
    SCENARIOS[name].apply(world)
    return world


__all__ = ["SCENARIOS", "Scenario", "build"]
