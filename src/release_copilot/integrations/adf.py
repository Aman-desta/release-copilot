"""Atlassian Document Format (ADF) <-> Markdown.

Jira Cloud v3 sends rich text (descriptions, comments) as ADF JSON trees. Agents read Markdown: it's compact,
models understand it well, and it keeps structure (lists, code) that plain text would lose.

- ``adf_to_markdown``: YOUR PART. Contract in tests/test_adf.py.
- ``markdown_to_adf``: provided. A deliberately small subset for the comments the copilot writes: headings,
  paragraphs, bullet and ordered lists, fenced code, and inline ``**bold**``, ``*italic*``, `` `code` ``, links.
"""

import re
from typing import Any

Node = dict[str, Any]

# ----------------------------------------------------------------------------------- ADF -> Markdown


def adf_to_markdown(doc: Node | None) -> str:
    """Render an ADF document as Markdown. Never raises: unknown or malformed nodes degrade to their text."""
    raise NotImplementedError


# ----------------------------------------------------------------------------------- Markdown -> ADF

_INLINE = re.compile(r"\*\*(?P<strong>[^*]+)\*\*|\*(?P<em>[^*]+)\*|`(?P<code>[^`]+)`|\[(?P<text>[^\]]+)\]\((?P<href>[^)\s]+)\)")
_LIST_ITEM = re.compile(r"^(?P<marker>[-*]|\d+\.)\s+(?P<text>.*)$")


def _inline(text: str) -> list[Node]:
    nodes: list[Node] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            nodes.append({"type": "text", "text": text[pos : m.start()]})
        if m.group("strong"):
            nodes.append({"type": "text", "text": m.group("strong"), "marks": [{"type": "strong"}]})
        elif m.group("em"):
            nodes.append({"type": "text", "text": m.group("em"), "marks": [{"type": "em"}]})
        elif m.group("code"):
            nodes.append({"type": "text", "text": m.group("code"), "marks": [{"type": "code"}]})
        else:
            nodes.append({"type": "text", "text": m.group("text"), "marks": [{"type": "link", "attrs": {"href": m.group("href")}}]})
        pos = m.end()
    if pos < len(text):
        nodes.append({"type": "text", "text": text[pos:]})
    return nodes


def _paragraph(text: str) -> Node:
    return {"type": "paragraph", "content": _inline(text)}


def markdown_to_adf(markdown: str) -> Node:
    """Convert the copilot's Markdown subset to an ADF document (what Jira's comment API requires)."""
    content: list[Node] = []
    lines = markdown.strip("\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("```"):
            language = line[3:].strip() or None
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence
            node: Node = {"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(code)}]}
            if language:
                node["attrs"] = {"language": language}
            content.append(node)
        elif m := re.match(r"^(#{1,6})\s+(.*)$", line):
            content.append({"type": "heading", "attrs": {"level": len(m.group(1))}, "content": _inline(m.group(2))})
            i += 1
        elif _LIST_ITEM.match(line):
            ordered = _LIST_ITEM.match(line).group("marker")[0].isdigit()  # type: ignore[union-attr]
            items: list[Node] = []
            while i < len(lines) and (m2 := _LIST_ITEM.match(lines[i])):
                items.append({"type": "listItem", "content": [_paragraph(m2.group("text"))]})
                i += 1
            content.append({"type": "orderedList" if ordered else "bulletList", "content": items})
        else:
            paragraph = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not _LIST_ITEM.match(lines[i]) and not lines[i].startswith(("#", "```")):
                paragraph.append(lines[i])
                i += 1
            content.append(_paragraph(" ".join(paragraph)))
    return {"type": "doc", "version": 1, "content": content}
