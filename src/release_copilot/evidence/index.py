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

import hashlib
import json
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
    def __init__(self) -> None:
        self._entries: dict[str, IndexEntry] = {}
        self._conflicts: set[str] = set()

    @classmethod
    def from_items(cls, items: Iterable[Any]) -> "EvidenceIndex":
        index = cls()
        for evidence_id, url, item in iter_evidence(list(items)):
            index._add(IndexEntry(evidence_id, evidence_id.split(":", 1)[0], url, item))
        return index

    @classmethod
    def from_evidence(cls, evidence: ReleaseEvidence) -> "EvidenceIndex":
        return cls.from_items([evidence])

    @classmethod
    def from_artifacts(cls, artifacts: Iterable[ToolArtifact]) -> "EvidenceIndex":
        index = cls()
        for artifact in artifacts:
            if not artifact.ok:
                continue
            artifact_index = cls.from_items(artifact.objects)
            for citation in artifact.evidence:
                if citation.id not in artifact_index:
                    raise ValueError(f"artifact {artifact.tool!r} cites evidence it did not return: {citation.id}")
            for entry in artifact_index:
                index._add(entry)
            for citation in artifact.evidence:
                entry = index._entries[citation.id]
                if entry.url is None and citation.url is not None:
                    index._entries[citation.id] = IndexEntry(entry.id, entry.source, citation.url, entry.item)
        return index

    def merge(self, other: "EvidenceIndex") -> "EvidenceIndex":
        merged = EvidenceIndex()
        for entry in self:
            merged._entries[entry.id] = entry
        merged._conflicts.update(self._conflicts)
        for entry in other:
            merged._add(entry)
        merged._conflicts.update(other._conflicts)
        return merged

    def get(self, evidence_id: str) -> IndexEntry | None:
        return self._entries.get(evidence_id)

    def __contains__(self, evidence_id: object) -> bool:
        return isinstance(evidence_id, str) and evidence_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[IndexEntry]:
        """Entries in id order."""
        return iter(self._entries[evidence_id] for evidence_id in self.ids)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    @property
    def conflicts(self) -> tuple[str, ...]:
        return tuple(sorted(self._conflicts))

    def missing(self, cited: Iterable[str]) -> list[str]:
        return sorted(set(cited) - self._entries.keys())

    def fingerprint(self) -> str:
        content = {
            "entries": [
                {
                    "id": entry.id,
                    "url": entry.url,
                    "item": entry.item.model_dump(mode="json"),
                }
                for entry in self
            ],
            "conflicts": self.conflicts,
        }
        serialized = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _add(self, entry: IndexEntry) -> None:
        current = self._entries.get(entry.id)
        if current is None:
            self._entries[entry.id] = entry
            return

        url = current.url if current.url is not None else entry.url
        if current.url is None and url is not None:
            current = IndexEntry(current.id, current.source, url, current.item)
            self._entries[entry.id] = current
        if entry.id in self._conflicts:
            return
        if _covers(current.item, entry.item):
            return
        if _covers(entry.item, current.item):
            self._entries[entry.id] = IndexEntry(entry.id, entry.source, url, entry.item)
            return

        self._conflicts.add(entry.id)


def _covers(a: BaseModel, b: BaseModel) -> bool:
    """Whether ``a`` contains every non-default field value present in ``b``."""
    if type(a) is not type(b):
        return False
    a_values = a.model_dump(exclude_defaults=True)
    b_values = b.model_dump(exclude_defaults=True)
    return all(name in a_values and a_values[name] == value for name, value in b_values.items())
