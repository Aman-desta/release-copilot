"""Spec for ``adf_to_markdown`` (story T2). Make these pass."""

import pytest
from release_copilot_mocks.scenarios import build

from release_copilot.integrations.adf import adf_to_markdown, markdown_to_adf


def doc(*content):
    return {"type": "doc", "version": 1, "content": list(content)}


def p(*inline):
    return {"type": "paragraph", "content": list(inline)}


def t(text, *marks):
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = [m if isinstance(m, dict) else {"type": m} for m in marks]
    return node


def li(*content):
    return {"type": "listItem", "content": list(content)}


def bullets(*items):
    return {"type": "bulletList", "content": list(items)}


# ----------------------------------------------------------------------------------------- blocks


def test_paragraphs_are_separated_by_a_blank_line():
    assert adf_to_markdown(doc(p(t("one")), p(t("two")))) == "one\n\ntwo"


def test_headings():
    assert adf_to_markdown(doc({"type": "heading", "attrs": {"level": 2}, "content": [t("Scope")]})) == "## Scope"


def test_bullet_and_ordered_lists():
    ordered = {"type": "orderedList", "content": [li(p(t("first"))), li(p(t("second")))]}
    assert adf_to_markdown(doc(bullets(li(p(t("a"))), li(p(t("b")))))) == "- a\n- b"
    assert adf_to_markdown(doc(ordered)) == "1. first\n2. second"
    assert adf_to_markdown(doc({**ordered, "attrs": {"order": 3}})) == "3. first\n4. second"


def test_nested_lists_are_indented():
    nested = bullets(li(p(t("a")), bullets(li(p(t("b"))))), li(p(t("c"))))
    assert adf_to_markdown(doc(nested)) == "- a\n  - b\n- c"


def test_code_block_with_language():
    block = {"type": "codeBlock", "attrs": {"language": "json"}, "content": [t('{"a": 1}')]}
    assert adf_to_markdown(doc(block)) == '```json\n{"a": 1}\n```'
    assert adf_to_markdown(doc({"type": "codeBlock", "content": [t("x = 1")]})) == "```\nx = 1\n```"


def test_blockquote_and_rule():
    quote = {"type": "blockquote", "content": [p(t("line one")), p(t("line two"))]}
    assert adf_to_markdown(doc(quote, {"type": "rule"})) == "> line one\n>\n> line two\n\n---"


# ----------------------------------------------------------------------------------------- inline


def test_marks():
    link = {"type": "link", "attrs": {"href": "https://jira/browse/PAY-102"}}
    rendered = adf_to_markdown(doc(p(t("bold", "strong"), t(" "), t("it", "em"), t(" "), t("x()", "code"), t(" "), t("old", "strike"), t(" "), t("PAY-102", link))))
    assert rendered == "**bold** *it* `x()` ~~old~~ [PAY-102](https://jira/browse/PAY-102)"


def test_link_wraps_other_marks():
    link = {"type": "link", "attrs": {"href": "https://x"}}
    assert adf_to_markdown(doc(p(t("see", "strong", link)))) == "[**see**](https://x)"


def test_hard_break_mention_emoji_and_inline_card():
    para = p(
        t("cc "),
        {"type": "mention", "attrs": {"id": "712020:dana-reyes", "text": "@Dana Reyes"}},
        {"type": "hardBreak"},
        {"type": "emoji", "attrs": {"shortName": ":rocket:", "text": "🚀"}},
        t(" "),
        {"type": "inlineCard", "attrs": {"url": "https://github.com/acme-fin/payments-api/pull/416"}},
    )
    assert adf_to_markdown(doc(para)) == "cc @Dana Reyes\n🚀 https://github.com/acme-fin/payments-api/pull/416"


# ------------------------------------------------------------------------------------- robustness


@pytest.mark.parametrize("value", [None, {}, {"type": "doc"}, {"type": "doc", "content": None}])
def test_empty_documents(value):
    assert adf_to_markdown(value) == ""


def test_unknown_nodes_degrade_to_their_text():
    panel = {"type": "panel", "attrs": {"panelType": "warning"}, "content": [p(t("Feature flag required"))]}
    table = {"type": "table", "content": [{"type": "tableRow", "content": [{"type": "tableCell", "content": [p(t("cell"))]}]}]}
    out = adf_to_markdown(doc(panel, table, {"type": "mediaSingle"}))
    assert "Feature flag required" in out and "cell" in out


def test_malformed_nodes_never_raise():
    assert adf_to_markdown(doc({"type": "paragraph", "content": "oops"}, {"type": "text"}, "junk", p(t("ok")))).endswith("ok")


# ------------------------------------------------------------------------------------- round trips


@pytest.mark.parametrize(
    "markdown",
    [
        "# Release 2.4.0\n\nShipped **8** tickets; see [PAY-102](https://jira/browse/PAY-102) and `CaptureController`.",
        "- one\n- two\n\n1. first\n2. second",
        '```json\n{"risk": "moderate"}\n```',
    ],
)
def test_round_trip_with_markdown_to_adf(markdown):
    assert adf_to_markdown(markdown_to_adf(markdown)) == markdown


def test_seeded_descriptions_render():
    issues = build("clean")["jira"]["issues"]
    for issue in issues.values():
        assert adf_to_markdown(issue["fields"]["description"]).strip()
    pay101 = adf_to_markdown(issues["PAY-101"]["fields"]["description"])
    assert "- Checkout returns 422 for expired tokens" in pay101.splitlines()
