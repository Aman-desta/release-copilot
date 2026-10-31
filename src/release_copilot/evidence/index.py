"""The evidence index: evidence id -> item. Every citation in a document, change request or judge verdict is
checked against it (A4, O stories), so it must be complete, unambiguous and deterministic.

YOUR PART. The contract is in tests/test_evidence.py (the ``index`` tests). Build it on ``iter_evidence``
(``evidence/model.py``), which already knows where citable items live inside domain objects.

Required behaviour:
- **Sources.** ``from_items`` (any domain objects, lists of them, sections), ``from_evidence`` (a
  ``ReleaseEvidence``) and ``from_artifacts`` (tool results, A2). ``from_artifacts`` skips failed artifacts
  (``ok=False``), and raises ``ValueError`` if an artifact lists an evidence id that none of its objects
  provides: a tool citing something it didn't return is a tool bug.
- **Lookups.** ``get(id)`` returns an ``IndexEntry`` or None. Also ``in``, ``len``, ``ids`` (sorted) and
  ``missing(cited)``: the cited ids that aren't in the index, sorted and without duplicates.
- **Merging the same id twice:**
  * equal items: one entry;
  * one is a **projection** of the other: keep the fuller one. A projection is what a narrow fetch returns
    (``jira_release_issues`` has no description, ``jira_get_issue`` has it). Item A covers item B when they are
    the same type and every field B sets to a non-default value is equal in A. Projections are not conflicts,
    and the result must not depend on the order the two arrived in;
  * anything else is a real **conflict**: keep the first one seen and record the id in ``conflicts`` (sorted).
    For example, the same metric read at two different times.
  * The URL is the first non-None one seen.
- **Immutable.** ``merge(other)`` returns a new index. Neither input changes.
- **``fingerprint()``**: a SHA-256 hex digest of the content (ids, urls, items as JSON, conflicts) that is equal
  for equal content, whatever the insertion order (for projections), and changes when any item changes.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from release_copilot.evidence.model import ReleaseEvidence, iter_evidence  # noqa: F401  (for your implementation)
from release_copilot.tools.base import ToolArtifact


@dataclass(frozen=True)
class IndexEntry:
    id: str  # e.g. jira:PAY-102
    source: str  # the id's prefix: jira | gh | sonar | azure | snow
    url: str | None
    item: BaseModel


class EvidenceIndex:
    @classmethod
    def from_items(cls, items: Iterable[Any]) -> "EvidenceIndex":
        raise NotImplementedError("EvidenceIndex: your part of A1")

    @classmethod
    def from_evidence(cls, evidence: ReleaseEvidence) -> "EvidenceIndex":
        raise NotImplementedError

    @classmethod
    def from_artifacts(cls, artifacts: Iterable[ToolArtifact]) -> "EvidenceIndex":
        raise NotImplementedError

    def merge(self, other: "EvidenceIndex") -> "EvidenceIndex":
        raise NotImplementedError

    def get(self, evidence_id: str) -> IndexEntry | None:
        raise NotImplementedError

    def __contains__(self, evidence_id: object) -> bool:
        raise NotImplementedError

    def __len__(self) -> int:
        raise NotImplementedError

    def __iter__(self) -> Iterator[IndexEntry]:
        """Entries in id order."""
        raise NotImplementedError

    @property
    def ids(self) -> tuple[str, ...]:
        raise NotImplementedError

    @property
    def conflicts(self) -> tuple[str, ...]:
        raise NotImplementedError

    def missing(self, cited: Iterable[str]) -> list[str]:
        raise NotImplementedError

    def fingerprint(self) -> str:
        raise NotImplementedError
