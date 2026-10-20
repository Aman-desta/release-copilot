"""Atlassian Document Format (ADF) helpers.

Jira Cloud REST v3 returns rich-text fields (description, comment bodies) as ADF JSON instead of plain text.
Clients must flatten it themselves (that's Phase 1 work), so the mock stores it the same way.
"""

from typing import Any


def doc(*paragraphs: str) -> dict[str, Any]:
    """Build an ADF document with one paragraph per string. A line starting with ``- `` becomes a bullet list."""
    content: list[dict[str, Any]] = []
    for para in paragraphs:
        lines = para.split("\n")
        if all(line.startswith("- ") for line in lines):
            content.append(
                {
                    "type": "bulletList",
                    "content": [
                        {"type": "listItem", "content": [_paragraph(line[2:])]} for line in lines
                    ],
                }
            )
        else:
            content.append(_paragraph(para))
    return {"type": "doc", "version": 1, "content": content}


def _paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def to_text(node: dict[str, Any] | None) -> str:
    """Flatten ADF to plain text. Used only by the mock (JQL ``text ~`` search); clients write their own."""
    if not node:
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    parts = [to_text(child) for child in node.get("content", [])]
    sep = "\n" if node.get("type") in {"doc", "bulletList"} else ""
    return sep.join(parts)
