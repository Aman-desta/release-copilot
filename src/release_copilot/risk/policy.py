"""The change policy: rules as data (``policies/change_policy.yaml``), validated when it is loaded.

A rule says *when* (a signal and a test on it) and *what* (block, add points, set a minimum tier, add a condition
to proceed). Rules only **add** risk: points can't be negative and there is no "maximum tier", so a policy can't
contain a rule that makes a release look safer. The signals are a closed set computed in code (``signals.py``):
a rule naming a signal that doesn't exist is a load error, not a rule that silently never fires.

**Unknown is not empty.** A rule's ``on_unknown`` says what happens when its signal couldn't be computed (the
evidence has a gap). The default is ``trigger``: fail closed, an unknown security scan blocks like a bad one.
A blocking rule *must* trigger: ``ignore`` is allowed only on rules that add points, a floor or a condition.
"""

import hashlib
import itertools
import re
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from release_copilot.risk.model import TIER_RANK, TIERS, Tier
from release_copilot.risk.signals import RiskSignals

Op = Literal["present", "equals", "at_most", "at_least", "in"]
_RULE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DEFAULT_POLICY_FILE = "change_policy.yaml"


class PolicyError(ValueError):
    """The policy file is invalid. The message names the rule and the field, never the file's other content."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Match(_Frozen):
    signal: str
    op: Op = "present"
    value: Any = None

    @model_validator(mode="after")
    def _check(self) -> "Match":
        if self.signal not in RiskSignals.model_fields:
            raise ValueError(f"unknown signal {self.signal!r}; known: {', '.join(RiskSignals.model_fields)}")
        if self.op == "present" and self.value is not None:
            raise ValueError("op 'present' takes no value")
        if self.op != "present" and self.value is None:
            raise ValueError(f"op {self.op!r} needs a value")
        if self.op in ("at_most", "at_least") and not isinstance(self.value, int | float):
            raise ValueError(f"op {self.op!r} needs a number")
        if self.op == "in" and not isinstance(self.value, list):
            raise ValueError("op 'in' needs a list")
        return self

    def matches(self, value: Any) -> bool:
        """Whether a **known** signal value matches. Callers handle unknown (None) themselves: it is a policy
        decision (``on_unknown``), not a match."""
        if value is None:
            raise ValueError("matches() is for known values; an unknown signal is decided by on_unknown")
        if self.op == "present":
            return bool(value)  # a non-empty tuple, True, a non-zero number
        if self.op == "equals":
            return value == self.value
        if self.op == "at_most":
            return value <= self.value
        if self.op == "at_least":
            return value >= self.value
        return value in self.value


class Rule(_Frozen):
    id: str
    reason: str = Field(min_length=1)
    when: Match
    block: bool = False
    score: int = Field(default=0, ge=0)  # rules only add risk
    min_tier: Tier | None = None
    # Non-empty: a blank condition would make a release "proceed with conditions" with nothing to check.
    condition: str | None = Field(default=None, min_length=1)
    on_unknown: Literal["trigger", "ignore"] = "trigger"

    @model_validator(mode="after")
    def _check(self) -> "Rule":
        if not _RULE_ID.fullmatch(self.id):
            raise ValueError(f"rule id {self.id!r} must be lower-case words joined by dashes")
        if not (self.block or self.score or self.min_tier or self.condition):
            raise ValueError(f"rule {self.id!r} has no effect (block, score, min_tier or condition)")
        if self.block and self.on_unknown == "ignore":
            # A blocker that ignores unknowns turns missing evidence into approval: the release passes because the
            # scan *timed out*. Unknown must never be safer than bad for a blocker.
            raise ValueError(f"rule {self.id!r} blocks, so it must fail closed (on_unknown: trigger)")
        return self


class ChangePolicy(_Frozen):
    version: str
    block_min_tier: Tier = "high"  # a blocked release is at least this risky
    score_tiers: dict[Tier, int]  # the lowest score for each tier above "low"
    rules: tuple[Rule, ...]

    @model_validator(mode="after")
    def _check(self) -> "ChangePolicy":
        if "low" in self.score_tiers:
            raise ValueError("score_tiers starts at 'moderate'; 'low' is every score below it")
        ordered = [self.score_tiers[t] for t in TIERS if t in self.score_tiers]
        if any(a >= b for a, b in itertools.pairwise(ordered)) or any(s <= 0 for s in ordered):
            raise ValueError("score_tiers must be positive and increase with the tier")
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate rule ids: {sorted({i for i in ids if ids.count(i) > 1})}")
        return self

    def tier_for_score(self, score: int) -> Tier:
        tier: Tier = "low"
        for candidate in TIERS:
            threshold = self.score_tiers.get(candidate)
            if threshold is not None and score >= threshold and TIER_RANK[candidate] > TIER_RANK[tier]:
                tier = candidate
        return tier


def load_policy(path: str | Path | None = None) -> ChangePolicy:
    """The policy at ``path``, or the packaged default. Raises ``PolicyError`` (never a half-loaded policy)."""
    source = Path(path) if path is not None else files("release_copilot.policies").joinpath(DEFAULT_POLICY_FILE)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))  # safe_load: a policy file can't construct objects
        return ChangePolicy.model_validate(raw)
    except yaml.YAMLError as exc:
        raise PolicyError(f"the policy is not valid YAML: {type(exc).__name__}") from None
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_input=False))
        raise PolicyError(f"invalid change policy: {problems}") from None


def policy_fingerprint(policy: ChangePolicy) -> str:
    return hashlib.sha256(policy.model_dump_json().encode()).hexdigest()[:16]
