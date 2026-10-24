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
    if not isinstance(doc, dict):
        return ""
    if doc.get("type") == "doc":
        return "\n\n".join(part for node in _children(doc) if (part := _render_block(node)))
    return _render_block(doc)


def _children(node: Node) -> list[Any]:
    content = node.get("content")
    return content if isinstance(content, list) else []


def _attrs(node: Node) -> dict[str, Any]:
    attrs = node.get("attrs")
    return attrs if isinstance(attrs, dict) else {}


def _render_inline(nodes: list[Any]) -> str:
    rendered: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type == "text":
            text = node.get("text")
            if not isinstance(text, str):
                continue
            marks = node.get("marks")
            valid_marks = [mark for mark in marks if isinstance(mark, dict)] if isinstance(marks, list) else []
            link = next((mark for mark in valid_marks if mark.get("type") == "link"), None)
            for mark in valid_marks:
                mark_type = mark.get("type")
                if mark_type == "strong":
                    text = f"**{text}**"
                elif mark_type == "em":
                    text = f"*{text}*"
                elif mark_type == "code":
                    text = f"`{text}`"
                elif mark_type == "strike":
                    text = f"~~{text}~~"
            if link is not None:
                href = _attrs(link).get("href")
                if isinstance(href, str) and href:
                    text = f"[{text}]({href})"
            rendered.append(text)
        elif node_type == "hardBreak":
            rendered.append("\n")
        elif isinstance(node_type, str) and node_type in {"mention", "emoji", "inlineCard"}:
            attrs = _attrs(node)
            if node_type == "mention":
                value = attrs.get("text") or attrs.get("displayName")
                if isinstance(value, str):
                    rendered.append(value if value.startswith("@") else f"@{value}")
            elif node_type == "emoji":
                value = attrs.get("text") or attrs.get("shortName")
                if isinstance(value, str):
                    rendered.append(value)
            else:
                value = attrs.get("url")
                if isinstance(value, str):
                    rendered.append(value)
        else:
            rendered.append(_plain_text(node))
    return "".join(rendered)


def _plain_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        text = node.get("text")
        return text if isinstance(text, str) else ""
    node_type = node.get("type")
    attrs = _attrs(node)
    if node_type == "mention":
        value = attrs.get("text") or attrs.get("displayName")
        return value if isinstance(value, str) else ""
    if node_type == "emoji":
        value = attrs.get("text") or attrs.get("shortName")
        return value if isinstance(value, str) else ""
    if node_type == "inlineCard":
        value = attrs.get("url")
        return value if isinstance(value, str) else ""
    if node_type == "hardBreak":
        return "\n"
    return "".join(_plain_text(child) for child in _children(node))


def _render_block(node: Any, list_depth: int = 0) -> str:
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    children = _children(node)
    if node_type == "paragraph":
        return _render_inline(children)
    if node_type == "heading":
        level = _attrs(node).get("level", 1)
        if isinstance(level, bool) or not isinstance(level, int):
            level = 1
        return f'{"#" * min(max(level, 1), 6)} {_render_inline(children)}'
    if isinstance(node_type, str) and node_type in {"bulletList", "orderedList"}:
        return _render_list(node, list_depth)
    if node_type == "listItem":
        return "\n\n".join(part for child in children if (part := _render_block(child, list_depth)))
    if node_type == "codeBlock":
        language = _attrs(node).get("language")
        language = language if isinstance(language, str) else ""
        code = "".join(_plain_text(child) for child in children)
        return f"```{language}\n{code}\n```"
    if node_type == "blockquote":
        body = "\n\n".join(part for child in children if (part := _render_block(child, list_depth)))
        if not body:
            return ""
        return "\n".join(f"> {line}" if line else ">" for line in body.split("\n"))
    if node_type == "rule":
        return "---"
    return _plain_text(node)


def _render_list(node: Node, depth: int) -> str:
    ordered = node.get("type") == "orderedList"
    order = _attrs(node).get("order", 1)
    if isinstance(order, bool) or not isinstance(order, int):
        order = 1

    lines: list[str] = []
    for child in _children(node):
        if not isinstance(child, dict) or child.get("type") != "listItem":
            continue
        marker = f"{order}. " if ordered else "- "
        order += 1
        prefix = " " * (depth * 2)
        continuation = prefix + " " * len(marker)
        first_line = True
        for item_child in _children(child):
            item_type = item_child.get("type") if isinstance(item_child, dict) else None
            if isinstance(item_type, str) and item_type in {"bulletList", "orderedList"}:
                nested = _render_list(item_child, depth + 1)
                if nested:
                    lines.extend(nested.split("\n"))
                continue

            content = _render_block(item_child, depth + 1)
            if not content:
                continue
            content_lines = content.split("\n")
            if first_line:
                lines.append(prefix + marker + content_lines[0])
                first_line = False
            else:
                lines.append(continuation + content_lines[0])
            lines.extend(continuation + line for line in content_lines[1:])
    return "\n".join(lines)


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
