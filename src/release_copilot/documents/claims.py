"""Reading a section body: its sentences, their citations, and the checkable claims in them.

These are deliberately **narrow**: a claim is checked only when its wording is unambiguous. "contains 8 commits"
states the commit count; "1 untracked commit" does not (it counts a subset), so it isn't matched, and "2 of 8
tickets" matches on "of 8", the total. A loose pattern that fires on the wrong sentence makes the validator reject
good documents, and a writer rejected for no reason learns nothing from the revision.
"""

import re
from dataclasses import dataclass

from release_copilot.documents.facts import CVE, VERSION

# One id per bracket; several brackets in a row for several ids. Prefixes: the evidence sources, facts and gaps.
CITATION = re.compile(r"\[((?:jira|gh|sonar|azure|snow|fact|gap):[^\[\]\s]+)\]")
# Anything shaped like a citation: "[word:...]", with any case, spacing or prefix. What matches this but not
# CITATION is a malformed citation ("[jira: PAY-1]", "[JIRA:PAY-1]", "[confluence:RB-12]"): it would render like
# a checked citation without being one, so the validator must see it instead of reading it as prose (fail closed).
CITATION_SHAPED = re.compile(r"\[\s*[A-Za-z][\w-]*\s*:[^\[\]]*\]")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
# Split after . ! ? followed by space, except after common abbreviations.
_BOUNDARY = re.compile(r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\betc\.)(?<!\bvs\.)(?<=[.!?])\s+")

_NUMBER_WORDS = {w: i for i, w in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"])}
# A total, not any number next to a noun: "contains 8 commits", "across 20 files", "all 8 tickets". "1 ticket is
# not done" or "2 of the commits" are about a subset, and are left alone.
_COUNT = re.compile(
    r"\b(?:contains|includes|ships|has|have|covers|spans|touches|changes|across|all|of|total of)\s+"
    r"(\d+|" + "|".join(_NUMBER_WORDS) + r")\s+(?:(?:jira|changed)\s+)?(commits?|tickets?|issues?|files?)\b",
    re.IGNORECASE,
)
COUNT_FACTS = {"commit": "commit_count", "ticket": "ticket_count", "issue": "ticket_count", "file": "files_changed"}
_GATE = re.compile(
    r"\bquality gate\s+(?:status\s+)?(?:is\s+|was\s+|has\s+|status:\s*)?(passed|passes|passing|failed|fails|failing|ok|error)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Sentence:
    text: str  # as written, citations included
    citations: tuple[str, ...]  # in order, duplicates kept
    malformed: tuple[str, ...] = ()  # citation-shaped brackets that aren't valid citations, as written

    @property
    def claim(self) -> str:
        """The sentence without its citations (valid or malformed): what is checked for counts, versions, CVEs and the gate."""
        return " ".join(CITATION_SHAPED.sub(" ", self.text).split())


def _sentence(text: str) -> Sentence:
    shaped = [m.group(0) for m in CITATION_SHAPED.finditer(text)]
    return Sentence(text, tuple(CITATION.findall(text)), tuple(b for b in shaped if not CITATION.fullmatch(b)))


def sentences(body: str) -> list[Sentence]:
    """The sentences of a section body. Each list item is its own sentence (or several).

    Citations written after the full stop ("Done. [jira:PAY-1]") belong to the sentence before them, so a
    fragment that is only citations is merged into the previous sentence instead of being one of its own.
    """
    found: list[Sentence] = []
    for line in body.splitlines():
        line = _LIST_MARKER.sub("", line).strip()
        if not line:
            continue
        for fragment in _BOUNDARY.split(line):
            fragment = fragment.strip()
            if not fragment:
                continue
            sentence = _sentence(fragment)
            if not sentence.claim.strip(" .!?") and found:
                sentence = _sentence(f"{found.pop().text} {fragment}")
            found.append(sentence)
    return found


def count_claims(text: str) -> list[tuple[str, int]]:
    """``[("commit_count", 8)]`` for "contains 8 commits": the fact a total is about, and the number stated."""
    claims = []
    for number, noun in _COUNT.findall(text):
        value = int(number) if number.isdigit() else _NUMBER_WORDS[number.lower()]
        claims.append((COUNT_FACTS[noun.lower().rstrip("s")], value))
    return claims


def gate_claims(text: str) -> list[str]:
    """The quality gate status a sentence states, as the gate reports it: "OK" or "ERROR"."""
    return ["OK" if word.lower().startswith(("pass", "ok")) else "ERROR" for word in _GATE.findall(text)]


def versions_in(text: str) -> list[str]:
    return VERSION.findall(text)


def cves_in(text: str) -> list[str]:
    return CVE.findall(text)
